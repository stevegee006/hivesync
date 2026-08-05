"""Request and response models for the auth endpoints.

There is no schema that carries a password back out, and none that exposes a
password hash.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    username: str
    must_change_password: bool


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class MessageResponse(BaseModel):
    detail: str
