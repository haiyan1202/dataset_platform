from __future__ import annotations

from sqlalchemy import select

from app.auth import hash_password
from app.db import SessionLocal
from app.models import Membership, Organization, User
from app.settings import get_settings
from app.storage import get_storage


def bootstrap() -> None:
    settings = get_settings()
    storage = get_storage()
    storage.ensure_bucket(settings.minio_bucket)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == settings.bootstrap_email.lower()))
        if user is None:
            user = User(email=settings.bootstrap_email.lower(), password_hash=hash_password(settings.bootstrap_password))
            db.add(user)
            db.flush()
        organization = db.scalar(select(Organization).where(Organization.name == settings.bootstrap_organization))
        if organization is None:
            organization = Organization(name=settings.bootstrap_organization)
            db.add(organization)
            db.flush()
        membership = db.get(Membership, {"organization_id": organization.id, "user_id": user.id})
        if membership is None:
            db.add(Membership(organization_id=organization.id, user_id=user.id, role="owner"))
        db.commit()
        print(f"Bootstrap complete: {user.email} / {organization.name}")


if __name__ == "__main__":
    bootstrap()


