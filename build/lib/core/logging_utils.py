import json, logging, sys, time
from pathlib import Path

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Allow extra fields via record.__dict__ (filtered)
        for k, v in record.__dict__.items():
            if k not in ("msg", "args", "exc_info", "exc_text", "stack_info", "stack_level", "created",
                         "msecs", "relativeCreated", "levelno", "levelname", "pathname", "filename",
                         "module", "lineno", "funcName", "thread", "threadName", "processName", "process"):
                try:
                    json.dumps({k: v})
                    payload[k] = v
                except Exception:
                    payload[k] = str(v)
        return json.dumps(payload)

def get_logger(name: str = "pqc") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    logger.propagate = False
    return logger


def configure_file_logger(role: str, logger: logging.Logger | None = None) -> Path:
    """Attach a JSON file handler and return log path."""

    active_logger = logger or get_logger()

    # Drop any previous file handlers we attached to avoid duplicate writes during tests.
    for handler in list(active_logger.handlers):
        if getattr(handler, "_pqc_file_handler", False):
            active_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = logs_dir / f"{role}-{timestamp}.log"

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler._pqc_file_handler = True  # type: ignore[attr-defined]
    active_logger.addHandler(file_handler)

    return path

# Very small metrics hook (no deps)
class Counter:
    def __init__(self): self.value = 0
    def inc(self, n: int = 1): self.value += n

class Gauge:
    def __init__(self): self.value = 0
    def set(self, v: float): self.value = v

class Metrics:
    def __init__(self):
        self.counters = {}
        self.gauges = {}
    def counter(self, name: str) -> Counter:
        self.counters.setdefault(name, Counter()); return self.counters[name]
    def gauge(self, name: str) -> Gauge:
        self.gauges.setdefault(name, Gauge()); return self.gauges[name]

METRICS = Metrics()
