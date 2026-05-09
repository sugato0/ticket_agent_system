from pydantic import BaseModel, EmailStr, Field
from typing import Optional

class LoginIn(BaseModel):
    username: str
    password: str

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_admin: bool

class RouteIn(BaseModel):
    description: str

class RouteOut(RouteIn):
    id: int
    class Config:
        from_attributes = True

class TaskIn(BaseModel):
    route_id: int
    dates: list[str] = Field(min_length=1)
    last_name: str
    first_name: str
    email: EmailStr
    phone: str
    user_priority: int = Field(default=1, ge=1)

class TaskUpdate(BaseModel):
    route_id: Optional[int] = None
    dates: Optional[list[str]] = None
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    user_priority: Optional[int] = Field(default=None, ge=1)

class AdminPriorityIn(BaseModel):
    admin_priority: int = 0
