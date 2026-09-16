"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.errors import unauthorized
from app.schemas import (
    AuthResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TelegramLoginRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth import AuthService, Principal, current_user, get_auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_meta(request: Request) -> dict[str, str | None]:
    forwarded = request.headers.get("x-forwarded-for")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else None
    )
    return {"user_agent": request.headers.get("user-agent"), "ip": ip}


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def register(
    payload: RegisterRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    meta = _client_meta(request)
    user = await service.register(
        email=payload.email, password=payload.password, display_name=payload.display_name
    )
    _, tokens = await service.login(
        email=payload.email, password=payload.password, **meta
    )
    return AuthResponse(user=_user(user), tokens=_tokens(tokens))


@router.post("/login", response_model=AuthResponse, summary="Log in with email and password")
async def login(
    payload: LoginRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    user, tokens = await service.login(
        email=payload.email, password=payload.password, **_client_meta(request)
    )
    return AuthResponse(user=_user(user), tokens=_tokens(tokens))


@router.post(
    "/telegram",
    response_model=AuthResponse,
    summary="Log in from a Telegram Mini App",
    description=(
        "Validates `Telegram.WebApp.initData` by HMAC-SHA256 against a key derived "
        "from the bot token. On first login the account is provisioned automatically. "
        "The signature covers the exact bytes of `init_data`, so it must be forwarded "
        "unmodified."
    ),
)
async def telegram_login(
    payload: TelegramLoginRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    user, tokens = await service.login_with_telegram(
        init_data=payload.init_data, **_client_meta(request)
    )
    return AuthResponse(user=_user(user), tokens=_tokens(tokens))


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token",
    description=(
        "Rotates the refresh token. The presented token is invalidated immediately. "
        "Reusing an already-rotated token is treated as theft and revokes the entire "
        "token family, so a stolen token cannot be used to keep a session alive."
    ),
)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    tokens = await service.refresh(
        refresh_token=payload.refresh_token, **_client_meta(request)
    )
    return _tokens(tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current token family",
)
async def logout(
    payload: RefreshRequest,
    service: AuthService = Depends(get_auth_service),
) -> Response:
    await service.logout(refresh_token=payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserResponse, summary="Current account and quota")
async def me(
    request: Request,
    principal: Principal = Depends(current_user),
) -> UserResponse:
    user = await request.app.state.repo.get_user(principal.user_id)
    if user is None:
        # The token verified but the row is gone: a deleted account.
        raise unauthorized("Account no longer exists")
    return _user(user)


def _user(user: dict) -> UserResponse:
    return UserResponse(
        id=str(user["id"]),
        email=user.get("email"),
        display_name=user.get("display_name") or "",
        role=user.get("role", "user"),
        status=user.get("status", "active"),
        quota_bytes=int(user.get("quota_bytes") or 0),
        used_bytes=int(user.get("used_bytes") or 0),
        created_at=user.get("created_at"),
    )


def _tokens(tokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )