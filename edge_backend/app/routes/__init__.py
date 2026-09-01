"""API routes package."""

import uuid
import logging
from typing import Callable
from fastapi import Request, Response
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("RouteErrorRecovery")

class ResilientRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()
        
        async def custom_route_handler(request: Request) -> Response:
            request_id = str(uuid.uuid4())
            request.state.request_id = request_id
            try:
                logger.info(f"[{request_id}] {request.method} {request.url}")
                response = await original_route_handler(request)
                return response
            except SQLAlchemyError as e:
                logger.error(f"[{request_id}] DB Error: {e}", exc_info=True)
                return JSONResponse(status_code=500, content={"error": "Database Error", "error_id": request_id, "detail": str(e)})
            except (OSError, FileNotFoundError) as e:
                logger.error(f"[{request_id}] File Error: {e}", exc_info=True)
                status_code = 404 if isinstance(e, FileNotFoundError) else 500
                return JSONResponse(status_code=status_code, content={"error": "File System Error", "error_id": request_id, "detail": str(e)})
            except Exception as e:
                from fastapi import HTTPException
                if isinstance(e, HTTPException):
                    raise e
                logger.error(f"[{request_id}] Unhandled Error: {e}", exc_info=True)
                return JSONResponse(status_code=500, content={"error": "Internal Server Error", "error_id": request_id, "detail": str(e)})
                
        return custom_route_handler
