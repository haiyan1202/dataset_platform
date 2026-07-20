from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.tokens import read_access_token
from app.db import get_db
from app.models import Membership, User

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth.required")
    user = db.get(User, uuid.UUID(read_access_token(credentials.credentials)))
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth.invalid_user")
    return user


def require_membership(db: Session, organization_id: uuid.UUID, user_id: uuid.UUID, *, write: bool = False) -> Membership:
    membership = db.get(Membership, {"organization_id": organization_id, "user_id": user_id})
    if membership is None or membership.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="organization.access_denied")
    if write and membership.role == "viewer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="organization.write_denied")
    return membership
