from time import sleep
from typing import Callable, TypeVar


T = TypeVar("T")


def build_retry_audit_payload() -> dict[str, object]:
    return {
        "retry_attempted": False,
        "retry_count": 0,
        "retry_candidate": None,
        "final_error_type": None,
        "final_error_message": None,
    }


def normalize_retry_audit_payload(payload: dict[str, object] | None) -> dict[str, object]:
    normalized = build_retry_audit_payload()
    if not isinstance(payload, dict):
        return normalized

    normalized["retry_attempted"] = bool(payload.get("retry_attempted", False))
    retry_count = payload.get("retry_count", 0)
    normalized["retry_count"] = retry_count if isinstance(retry_count, int) and retry_count >= 0 else 0

    retry_candidate = payload.get("retry_candidate")
    if isinstance(retry_candidate, bool):
        normalized["retry_candidate"] = retry_candidate

    final_error_type = payload.get("final_error_type")
    if isinstance(final_error_type, str):
        normalized["final_error_type"] = final_error_type

    final_error_message = payload.get("final_error_message")
    if isinstance(final_error_message, str):
        normalized["final_error_message"] = final_error_message

    return normalized


def merge_retry_audit_payload(
    target: dict[str, object],
    source: dict[str, object] | None,
) -> dict[str, object]:
    normalized_target = normalize_retry_audit_payload(target)
    normalized_source = normalize_retry_audit_payload(source)

    normalized_target["retry_attempted"] = bool(normalized_target["retry_attempted"]) or bool(
        normalized_source["retry_attempted"]
    )
    normalized_target["retry_count"] = int(normalized_target["retry_count"]) + int(normalized_source["retry_count"])

    target_candidate = normalized_target["retry_candidate"]
    source_candidate = normalized_source["retry_candidate"]
    if target_candidate is True or source_candidate is True:
        normalized_target["retry_candidate"] = True
    elif target_candidate is False or source_candidate is False:
        normalized_target["retry_candidate"] = False
    else:
        normalized_target["retry_candidate"] = None

    normalized_target["final_error_type"] = normalized_source["final_error_type"]
    normalized_target["final_error_message"] = normalized_source["final_error_message"]
    return normalized_target


def read_provider_retry_report(provider: object | None) -> dict[str, object]:
    if provider is None:
        return build_retry_audit_payload()
    report_getter = getattr(provider, "get_retry_report", None)
    if callable(report_getter):
        return normalize_retry_audit_payload(report_getter())
    return build_retry_audit_payload()


def reset_provider_retry_report(provider: object | None) -> None:
    if provider is None:
        return
    resetter = getattr(provider, "reset_retry_report", None)
    if callable(resetter):
        resetter()


def run_with_retry(
    operation: Callable[[], T],
    classify_error: Callable[[Exception], dict[str, object]],
    *,
    max_retries: int,
    backoff_seconds: float,
    should_retry_error: Callable[[Exception, dict[str, object]], bool] | None = None,
) -> tuple[T, dict[str, object]]:
    retry_report = build_retry_audit_payload()
    retry_count = 0

    while True:
        try:
            result = operation()
            retry_report["final_error_type"] = None
            retry_report["final_error_message"] = None
            return result, retry_report
        except Exception as exc:
            error_info = classify_error(exc)
            error_type = str(error_info.get("error_type", "provider_error"))
            retry_candidate = bool(error_info.get("retry_candidate", False))
            normalized_error_info = {
                "error_type": error_type,
                "retry_candidate": retry_candidate,
            }
            if should_retry_error is not None:
                retry_candidate = retry_candidate and should_retry_error(exc, normalized_error_info)

            retry_report["retry_candidate"] = retry_candidate
            retry_report["final_error_type"] = error_type
            retry_report["final_error_message"] = str(exc)

            if not retry_candidate or retry_count >= max_retries:
                setattr(exc, "retry_report", dict(retry_report))
                raise

            retry_count += 1
            retry_report["retry_attempted"] = True
            retry_report["retry_count"] = retry_count
            if backoff_seconds > 0:
                sleep(backoff_seconds * retry_count)
