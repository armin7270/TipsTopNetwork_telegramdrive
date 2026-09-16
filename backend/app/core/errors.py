"""RFC 9457 ``application/problem+json`` error handling.

Errors carry both a human-readable ``detail`` and a stable machine-readable
``code``. Clients (notably the Android transfer engine) branch on ``code``, so
the codes are part of the public contract and must not be renamed casually.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_BASE_URI = "https://teledrive.dev/problems"


class ProblemError(Exception):
    """An error that maps directly onto an RFC 9457 problem document."""

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        title: str | None = None,
        headers: dict[str, str] | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.title = title or _DEFAULT_TITLES.get(status_code, "Error")
        self.headers = headers or {}
        self.extensions = extensions or {}

    def to_document(self, instance: str) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "type": f"{PROBLEM_BASE_URI}/{self.code}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            "instance": instance,
            "code": self.code,
            "extensions": dict(self.extensions),
        }
        doc.update(self.extensions)
        return doc

    def to_response(self, instance: str) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=self.to_document(instance),
            media_type=PROBLEM_CONTENT_TYPE,
            headers=self.headers,
        )


_DEFAULT_TITLES: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    410: "Gone",
    413: "Payload Too Large",
    416: "Range Not Satisfiable",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    507: "Insufficient Storage",
}


# --- Named constructors for every code the API can emit ----------------------
# Centralised so the OpenAPI spec, the Android client, and the tests all refer to
# the same strings.

def unauthorized(detail: str = "Authentication required") -> ProblemError:
    return ProblemError(401, "unauthorized", detail, headers={"WWW-Authenticate": "Bearer"})


def invalid_credentials(detail: str = "Invalid email or password") -> ProblemError:
    return ProblemError(401, "invalid_credentials", detail)


def token_expired(detail: str = "Access token is expired") -> ProblemError:
    return ProblemError(401, "token_expired", detail, headers={"WWW-Authenticate": "Bearer"})


def refresh_reuse_detected(detail: str = "Refresh token reuse detected; session revoked") -> ProblemError:
    return ProblemError(401, "refresh_reuse_detected", detail)


def forbidden(detail: str = "You do not have access to this resource") -> ProblemError:
    return ProblemError(403, "forbidden", detail)


def not_found(detail: str = "Resource not found") -> ProblemError:
    return ProblemError(404, "not_found", detail)


def invalid_argument(detail: str) -> ProblemError:
    """A syntactically valid request whose content is not acceptable.

    Distinct from a schema-validation failure (400 ``validation_failed``): this is
    for values that parse correctly but cannot be honoured, such as a
    ``parent_id`` that names a file rather than a folder.
    """
    return ProblemError(400, "invalid_argument", detail)


def precondition_failed(detail: str) -> ProblemError:
    return ProblemError(412, "precondition_failed", detail)


def node_name_conflict(name: str) -> ProblemError:
    return ProblemError(409, "node_name_conflict", f"A node named {name!r} already exists here")


def email_conflict(identifier: str) -> ProblemError:
    """Raised when a unique identity column already holds this value.

    The message deliberately does not confirm which account exists: the auth layer
    converts this into a generic failure so registration cannot be used to
    enumerate users.
    """
    return ProblemError(409, "email_conflict", f"{identifier!r} is already in use")


def node_cycle(detail: str = "A folder cannot be moved into its own descendant") -> ProblemError:
    return ProblemError(409, "node_cycle", detail)


def folder_not_empty(detail: str = "Folder is not empty; pass recursive=true to delete it") -> ProblemError:
    return ProblemError(409, "folder_not_empty", detail)


def root_immutable(detail: str = "The root node cannot be renamed, moved, or deleted") -> ProblemError:
    return ProblemError(409, "root_immutable", detail)


def quota_exceeded(used: int, quota: int, requested: int) -> ProblemError:
    return ProblemError(
        507,
        "quota_exceeded",
        f"Storage quota exceeded: {used} used of {quota}, {requested} requested",
        extensions={"used_bytes": used, "quota_bytes": quota, "requested_bytes": requested},
    )


def chunk_hash_mismatch(expected: str, actual: str, *, chunk_index: int | None = None) -> ProblemError:
    extensions: dict[str, Any] = {
        "expected_sha256": expected,
        "actual_sha256": actual,
    }
    if chunk_index is not None:
        extensions["chunk_index"] = chunk_index
    return ProblemError(
        400,
        "chunk_hash_mismatch",
        f"Declared X-Chunk-SHA256 {expected} does not match received {actual}",
        extensions=extensions,
    )


def chunk_too_large(limit: int, received: int) -> ProblemError:
    return ProblemError(
        413,
        "chunk_too_large",
        f"Chunk body exceeds the session chunk size of {limit} bytes",
        extensions={"chunk_size": limit, "received_bytes": received},
    )


def chunk_already_uploaded(chunk_index: int) -> ProblemError:
    return ProblemError(
        409,
        "chunk_already_uploaded",
        f"Chunk {chunk_index} was already uploaded with different content",
        extensions={"chunk_index": chunk_index},
    )


def chunk_index_out_of_range(chunk_index: int, total_chunks: int) -> ProblemError:
    return ProblemError(
        416,
        "chunk_index_out_of_range",
        f"Chunk index {chunk_index} is outside 0..{total_chunks - 1}",
        extensions={"chunk_index": chunk_index, "total_chunks": total_chunks},
    )


def upload_expired(upload_id: str) -> ProblemError:
    return ProblemError(410, "upload_expired", f"Upload session {upload_id} has expired")


def upload_incomplete(missing: list[int]) -> ProblemError:
    return ProblemError(
        409,
        "upload_incomplete",
        f"{len(missing)} chunk(s) are still missing",
        extensions={"missing_chunks": missing},
    )


def size_mismatch(expected: int, received: int) -> ProblemError:
    return ProblemError(
        422,
        "size_mismatch",
        f"Received {received} bytes but the session declared {expected}",
        extensions={"expected_bytes": expected, "received_bytes": received},
    )


def file_hash_mismatch(expected: str, actual: str) -> ProblemError:
    return ProblemError(
        400,
        "file_hash_mismatch",
        f"Whole-file SHA-256 {actual} does not match the declared {expected}",
        extensions={"expected_sha256": expected, "actual_sha256": actual},
    )


def chunk_unreadable(chunk_index: int, detail: str = "") -> ProblemError:
    return ProblemError(
        502,
        "chunk_unreadable",
        detail or f"Chunk {chunk_index} could not be retrieved from any storage location",
        extensions={"chunk_index": chunk_index},
    )


def range_not_satisfiable(size_bytes: int) -> ProblemError:
    return ProblemError(
        416,
        "range_not_satisfiable",
        f"Requested range is outside the file of {size_bytes} bytes",
        headers={"Content-Range": f"bytes */{size_bytes}"},
        extensions={"size_bytes": size_bytes},
    )


def rate_limited(retry_after_seconds: int, detail: str = "Too many requests") -> ProblemError:
    return ProblemError(
        429,
        "rate_limited",
        detail,
        headers={"Retry-After": str(max(1, retry_after_seconds))},
    )


def thumbnail_unavailable(detail: str = "No thumbnail is available for this node") -> ProblemError:
    return ProblemError(404, "thumbnail_unavailable", detail)


def invalid_init_data(detail: str = "Telegram initData failed validation") -> ProblemError:
    return ProblemError(401, "invalid_init_data", detail)


def storage_unavailable(detail: str = "Telegram storage is temporarily unavailable") -> ProblemError:
    return ProblemError(503, "storage_unavailable", detail)


def validation_failed(errors: list[dict[str, Any]]) -> ProblemError:
    return ProblemError(
        422,
        "validation_failed",
        "One or more fields failed validation",
        extensions={"errors": errors},
    )


def _instance_of(request: Request) -> str:
    return str(request.url.path)


def install_error_handlers(app: FastAPI) -> None:
    """Wire problem+json responses for every failure path.

    Without this, FastAPI's default handlers would emit ``{"detail": ...}`` for
    some failures and problem+json for others, forcing clients to parse two
    shapes. Everything is normalised here.
    """

    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return exc.to_response(_instance_of(request))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [
            {
                "loc": [str(p) for p in e.get("loc", ())],
                "msg": e.get("msg", ""),
                "type": e.get("type", ""),
            }
            for e in exc.errors()
        ]
        return validation_failed(errs).to_response(_instance_of(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        headers = dict(getattr(exc, "headers", None) or {})
        return ProblemError(
            exc.status_code,
            code,
            str(exc.detail),
            headers=headers,
        ).to_response(_instance_of(request))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals: the detail is generic, the correlation id is what
        # an operator uses to find the real traceback in the logs.
        import logging

        logging.getLogger("teledrive").exception("unhandled error", exc_info=exc)
        err = ProblemError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred",
        )
        return err.to_response(_instance_of(request))