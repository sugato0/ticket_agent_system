import hashlib
import os
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from .database import get_db
from .models import User

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-real-project")
ALGORITHM = "HS256"
TOKEN_LIFETIME_HOURS = 12

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(password: str) -> str:
    """Хеш пароля без внешнего bcrypt.

    Для учебной работы этого достаточно: пароль не хранится открытым текстом,
    а проект одинаково запускается в Docker на Python 3.12. В настоящем сервисе
    я бы заменил это на argon2 или bcrypt с нормально закрепленной версией пакета.
    """
    password_bytes = password.encode("utf-8")
    digest = hashlib.sha256(password_bytes).hexdigest()
    return f"sha256${digest}"


def verify_password(password: str, saved_hash: str) -> bool:
    return secrets.compare_digest(hash_password(password), saved_hash)


def create_token(user: User) -> str:
    expires_at = datetime.utcnow() + timedelta(hours=TOKEN_LIFETIME_HOURS)
    payload = {
        "sub": user.username,
        "is_admin": user.is_admin,
        "exp": expires_at,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    if not username:
        raise HTTPException(status_code=401, detail="Token has no user")

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user
