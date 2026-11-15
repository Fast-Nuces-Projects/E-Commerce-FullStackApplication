from fastapi import APIRouter, Depends, HTTPException, status
from datetime import datetime
from ..database import db_dependency
from .. import models
from ..auth import get_current_user, require_admin

router = APIRouter(prefix="/payments", tags=["Payments"])


@router.get("/methods")
async def list_payment_methods(current_user: models.User = Depends(get_current_user)):
	# Declared before "/{order_id}" so "methods" is not parsed as an order id.
	return [
		{"value": m.value, "label": models.PAYMENT_METHOD_LABELS[m]}
		for m in models.ENABLED_PAYMENT_METHODS
	]


@router.post("/{order_id}")
async def pay_order(order_id: int, db: db_dependency, current_user: models.User = Depends(get_current_user)):
	order = db.query(models.Order).filter(models.Order.id == order_id).first()
	if not order:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
	if order.user_id != current_user.id and current_user.role.name != "ADMIN":
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to pay for this order")

	# Idempotency: if already has payment completed, return
	if order.payment and order.payment.status.name == "COMPLETED":
		return {"message": "Payment already completed", "transaction_id": order.payment.transaction_id}

	payment = order.payment
	if not payment:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
	if payment.method == models.PaymentMethod.CASH:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Cash on delivery orders are paid on delivery",
		)

	# Mock payment processing: mark as completed
	payment.status = models.PaymentStatus.COMPLETED
	payment.transaction_id = payment.transaction_id or f"TXN-{order.id}-{int(datetime.utcnow().timestamp())}"
	db.commit()
	db.refresh(payment)

	# Create invoice if absent
	if not order.invoice:
		invoice = models.Invoice(order_id=order.id, amount=order.amount, invoice_date=datetime.utcnow())
		db.add(invoice)
		db.commit()

	# Update order status
	order.status = models.OrderStatus.SHIPPED
	db.commit()

	return {
		"order_id": order.id,
		"payment_status": payment.status.value,
		"transaction_id": payment.transaction_id,
	}


@router.get("/{order_id}")
async def get_payment(order_id: int, db: db_dependency, current_user: models.User = Depends(get_current_user)):
	order = db.query(models.Order).filter(models.Order.id == order_id).first()
	if not order:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
	if order.user_id != current_user.id and current_user.role.name != "ADMIN":
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
	if not order.payment:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
	return {
		"order_id": order.id,
		"amount": str(order.payment.amount),
		"status": order.payment.status.value,
		"transaction_id": order.payment.transaction_id,
	}

