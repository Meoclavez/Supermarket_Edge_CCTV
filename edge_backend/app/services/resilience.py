import asyncio
import json
import logging
import time
from datetime import datetime
from functools import wraps
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Dict, Optional, Type
import sys
import traceback

class CircuitBreaker:
    """
    A circuit breaker pattern implementation to prevent cascading failures.
    States:
      - CLOSED: Requests pass through freely.
      - OPEN: Requests fail immediately (cooldown).
      - HALF_OPEN: One request allowed to probe if the service is recovered.
    """
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, name: str = "CircuitBreaker", failure_threshold: int = 5, cooldown_seconds: int = 30, recovery_timeout: Optional[float] = None, **kwargs):
        if isinstance(name, int):
            self.failure_threshold = name
            self.name = kwargs.get("name", "CircuitBreaker")
        else:
            self.name = name
            self.failure_threshold = failure_threshold
        self.cooldown_seconds = int(recovery_timeout) if recovery_timeout is not None else cooldown_seconds
        self.state = self.CLOSED
        self.failures = 0
        self.last_failure_time = 0.0

    def can_execute(self) -> bool:
        """Check if execution is permitted under the current circuit state."""
        if self.state == self.OPEN:
            if time.time() - self.last_failure_time > self.cooldown_seconds:
                self.state = self.HALF_OPEN
                logging.info(f"[{self.name}] Circuit half-open, probing...")
                return True
            return False
        return True

    def record_success(self):
        """Record successful execution and close the circuit."""
        if self.state == self.HALF_OPEN:
            logging.info(f"[{self.name}] Circuit recovered, closed.")
        self.state = self.CLOSED
        self.failures = 0

    def record_failure(self):
        """Record failure and trip circuit to OPEN if threshold exceeded."""
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.failure_threshold and self.state != self.OPEN:
            self.state = self.OPEN
            logging.warning(f"[{self.name}] Circuit tripped to OPEN (threshold {self.failure_threshold} reached).")

    async def __aenter__(self):
        if not self.can_execute():
            raise Exception(f"[{self.name}] Circuit is OPEN. Request rejected.")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.record_failure()
        else:
            self.record_success()
        return False

    def __call__(self, func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with self:
                return await func(*args, **kwargs)
        return wrapper


def RetryWithBackoff(max_retries: int = 3, base_delay: float = 0.5, max_delay: float = 30.0, retryable_exceptions: tuple = (Exception,)):
    """
    Async decorator that retries a function with exponential backoff and jitter.
    """
    import random
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    if attempt == max_retries:
                        logging.error(f"[{func.__name__}] Failed after {max_retries} retries: {str(e)}")
                        raise e
                    
                    jitter = random.uniform(0, 0.1 * delay)
                    sleep_time = min(delay + jitter, max_delay)
                    logging.warning(f"[{func.__name__}] Attempt {attempt + 1} failed ({str(e)}). Retrying in {sleep_time:.2f}s...")
                    
                    await asyncio.sleep(sleep_time)
                    delay *= 2
        return wrapper
    return decorator


class ServiceHealthTracker:
    """
    Singleton tracker for health status of different backend subsystems.
    """
    _instance = None
    
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ServiceHealthTracker, cls).__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.services = {
            "hailo": self._default_status(),
            "rtsp_cam_0": self._default_status(),
            "go2rtc": self._default_status(),
            "database": self._default_status(),
            "notification": self._default_status()
        }

    def _default_status(self) -> Dict[str, Any]:
        return {
            "status": self.HEALTHY,
            "last_error": None,
            "last_success_time": time.time(),
            "consecutive_failures": 0
        }

    @classmethod
    def report_status(cls, service_name: str, status_str: str, message: Optional[str] = None):
        """Classmethod helper to report subsystem health status."""
        instance = cls()
        if service_name not in instance.services:
            instance.services[service_name] = instance._default_status()
        s = instance.services[service_name]
        s["status"] = status_str.upper()
        if message:
            s["last_error"] = message
        if status_str.lower() in ("healthy", "ok"):
            s["last_success_time"] = time.time()
            s["consecutive_failures"] = 0
            s["last_error"] = None
        else:
            s["consecutive_failures"] = s.get("consecutive_failures", 0) + 1

    def record_success(self, service_name: str):
        self.report_status(service_name, self.HEALTHY)

    def record_failure(self, service_name: str, error: str):
        self.report_status(service_name, self.DEGRADED, error)

    def get_system_health_report(self) -> Dict[str, Any]:
        return {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "services": self.services
        }


class StructuredJsonFormatter(logging.Formatter):
    """
    Format logs as JSON. Include timestamp, level, message, and optional extra fields.
    """
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger_name": record.name,
            "message": record.getMessage(),
        }
        
        # Add standard optional fields if present in extra
        optional_fields = ["camera_id", "event_id", "service", "error_type", "latency_ms"]
        for field in optional_fields:
            if hasattr(record, field):
                log_obj[field] = getattr(record, field)
                
        if record.exc_info:
            log_obj["traceback"] = self.formatException(record.exc_info)
            
        return json.dumps(log_obj)

def setup_structured_logging():
    """
    Configures Python logging to output structured JSON to both stdout and a rotating file.
    """
    import os
    
    # Create logs directory
    os.makedirs("storage/logs", exist_ok=True)
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Remove existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        
    formatter = StructuredJsonFormatter()
    
    # Rotating File Handler (10MB, 5 backups)
    file_handler = RotatingFileHandler(
        "storage/logs/edge_cctv.log", maxBytes=10*1024*1024, backupCount=5
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # Stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)
    
    logging.info("Structured logging initialized.")
