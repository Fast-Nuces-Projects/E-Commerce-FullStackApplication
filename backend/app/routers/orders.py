from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import joinedload

from ..database import db_dependency
from .. import models, schemas
from ..auth import get_current_user, require_admin

router = APIRouter(prefix="/orders", tags=["Orders"])


def _order_to_dict(o: models.Order, include_user: bool = False) -> dict:
    payment = o.payment
    data = {
        "id": o.id,
        "amount": str(o.amount),
        "status": o.status.value,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "payment_method": payment.method.value if payment else None,
        "payment_method_label": models.PAYMENT_METHOD_LABELS[payment.method] if payment else None,
        "payment_status": payment.status.value if payment and payment.status else None,
        "items": [
            {"product_id": it.product_id, "quantity": it.quantity, "price": str(it.price)} for it in o.items
        ],
    }
    if include_user:
        data["user_id"] = o.user_id
    return data


@router.post("", status_code=status.HTTP_201_CREATED, response_model=schemas.OrderResponse)
async def create_order_from_cart(
    body: schemas.OrderCreate,
    db: db_dependency = None,
    current_user: models.User = Depends(get_current_user),
):
    method = models.PaymentMethod(body.payment_method.value)
    if method not in models.ENABLED_PAYMENT_METHODS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment method not available")

    cart = db.query(models.Cart).filter(models.Cart.user_id == current_user.id).first()
    if not cart:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cart is empty")

    cart_items = db.query(models.CartItem).filter(models.CartItem.cart_id == cart.id).all()
    if not cart_items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No items in cart")

    total_amount = Decimal("0.00")
    products_map = {}

    for ci in cart_items:
        # Lock the product row so concurrent checkouts cannot oversell stock.
        product = (
            db.query(models.Product)
            .filter(models.Product.id == ci.product_id)
            .with_for_update()
            .first()
        )
        if not product:
            raise HTTPException(status_code=400, detail=f"Product {ci.product_id} not found")
        if product.stock is None or product.stock < ci.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for product {product.id}")
        total_amount += Decimal(str(product.price)) * ci.quantity
        products_map[ci.product_id] = product

    # Build the order, payment and items, then commit once so a failure leaves nothing behind.
    try:
        order = models.Order(user_id=current_user.id, amount=total_amount)
        db.add(order)
        db.flush()

        # Cash on delivery stays PENDING until the admin confirms delivery.
        payment = models.Payment(
            order_id=order.id,
            amount=total_amount,
            method=method,
            status=models.PaymentStatus.PENDING,
        )
        db.add(payment)

        for ci in cart_items:
            product = products_map[ci.product_id]
            product.stock -= ci.quantity
            db.add(models.OrderItem(order_id=order.id, product_id=ci.product_id, quantity=ci.quantity, price=product.price))
            db.delete(ci)

        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(order)

    return {
        "order_id": order.id,
        "amount": order.amount,
        "status": order.status,
        "payment_method": method.value,
        "payment_status": payment.status.value,
    }


@router.get("", dependencies=[Depends(require_admin)])
async def list_orders(db: db_dependency):
    orders = (
        db.query(models.Order)
        .options(joinedload(models.Order.items), joinedload(models.Order.payment))
        .all()
    )
    return [_order_to_dict(o, include_user=True) for o in orders]


@router.get("/me")
async def list_my_orders(db: db_dependency, current_user: models.User = Depends(get_current_user)):
    orders = (
        db.query(models.Order)
        .filter(models.Order.user_id == current_user.id)
        .options(joinedload(models.Order.items), joinedload(models.Order.payment))
        .all()
    )
    return [_order_to_dict(o) for o in orders]


@router.post("/{order_id}/deliver", dependencies=[Depends(require_admin)])
async def mark_order_delivered(order_id: int, db: db_dependency):
    """Admin action: mark a cash-on-delivery order delivered and its cash payment collected."""
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.status in (models.OrderStatus.DELIVERED, models.OrderStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order is already {order.status.value.lower()}",
        )
    if not order.payment or order.payment.method != models.PaymentMethod.CASH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only cash on delivery orders can be marked delivered here",
        )

    now = datetime.utcnow()
    order.status = models.OrderStatus.DELIVERED
    order.payment.status = models.PaymentStatus.COMPLETED
    if not order.invoice:
        db.add(models.Invoice(order_id=order.id, amount=order.amount, invoice_date=now, shipping_date=now))
    else:
        order.invoice.shipping_date = now
    db.commit()

    return {
        "order_id": order.id,
        "status": order.status.value,
        "payment_status": order.payment.status.value,
    }


@router.get("/{order_id}")
async def get_order(order_id: int, db: db_dependency, current_user: models.User = Depends(get_current_user)):
    order = (
        db.query(models.Order)
        .filter(models.Order.id == order_id)
        .options(joinedload(models.Order.items), joinedload(models.Order.payment))
        .first()
    )
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.user_id != current_user.id and current_user.role.name != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to view this order")
    return _order_to_dict(order, include_user=True)
