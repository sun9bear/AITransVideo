# ============================================================
# 将以下代码追加到 FC relay 的 app.py 中
# （在现有的 /send 路由和 /health 路由之间或之后）
# ============================================================

@app.route("/captcha-verify", methods=["POST"])
def captcha_verify():
    """Verify an Aliyun Captcha 2.0 token via VerifyIntelligentCaptcha.

    The China captcha endpoint (captcha.cn-shanghai.aliyuncs.com) is only
    reachable from within China. This FC function in HK can reach it.

    Request JSON:
      {
        "captcha_token": "<captchaVerifyParam from frontend SDK>",
        "scene_id": "<captcha scene ID>"
      }

    Response JSON:
      { "ok": true,  "verify_code": "T001" }   — verification passed
      { "ok": false, "verify_code": "F002", "error": "..." } — failed
    """
    import hashlib
    import hmac
    import json
    import os
    import time
    import urllib.parse
    import urllib.request
    import uuid as uuid_mod

    data = request.get_json(force=True) or {}
    captcha_token = (data.get("captcha_token") or "").strip()
    scene_id = (data.get("scene_id") or "").strip()

    if not captcha_token or not scene_id:
        return jsonify({"ok": False, "error": "missing captcha_token or scene_id"}), 400

    access_key_id = os.environ.get("ACCESS_KEY_ID", "").strip()
    access_key_secret = os.environ.get("ACCESS_KEY_SECRET", "").strip()

    if not access_key_id or not access_key_secret:
        return jsonify({"ok": False, "error": "server captcha config missing"}), 500

    # China endpoint — reachable from HK FC
    endpoint = "captcha.cn-shanghai.aliyuncs.com"

    body_params = {"CaptchaVerifyParam": captcha_token, "SceneId": scene_id}
    body_str = json.dumps(body_params)
    body_bytes = body_str.encode("utf-8")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    nonce = uuid_mod.uuid4().hex

    headers_to_sign = {
        "content-type": "application/json",
        "host": endpoint,
        "x-acs-action": "VerifyIntelligentCaptcha",
        "x-acs-content-sha256": hashlib.sha256(body_bytes).hexdigest(),
        "x-acs-date": now,
        "x-acs-signature-nonce": nonce,
        "x-acs-version": "2023-03-05",
    }

    signed_headers_str = ";".join(sorted(headers_to_sign.keys()))
    canonical_headers = "".join(
        f"{k}:{headers_to_sign[k]}\n" for k in sorted(headers_to_sign.keys())
    )
    canonical_request = "\n".join([
        "POST", "/", "", canonical_headers, signed_headers_str,
        hashlib.sha256(body_bytes).hexdigest(),
    ])
    string_to_sign = "ACS3-HMAC-SHA256\n" + hashlib.sha256(
        canonical_request.encode("utf-8")
    ).hexdigest()
    signature = hmac.new(
        access_key_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    authorization = (
        f"ACS3-HMAC-SHA256 Credential={access_key_id},"
        f"SignedHeaders={signed_headers_str},"
        f"Signature={signature}"
    )

    req_headers = {
        "Content-Type": "application/json",
        "Host": endpoint,
        "x-acs-action": "VerifyIntelligentCaptcha",
        "x-acs-content-sha256": hashlib.sha256(body_bytes).hexdigest(),
        "x-acs-date": now,
        "x-acs-signature-nonce": nonce,
        "x-acs-version": "2023-03-05",
        "Authorization": authorization,
    }

    url = f"https://{endpoint}/"
    req = urllib.request.Request(url, data=body_bytes, method="POST", headers=req_headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return jsonify({"ok": False, "error": f"captcha API HTTP {exc.code}: {err_body[:200]}"}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": f"captcha API error: {exc}"}), 502

    verify_result = result.get("Result", {})
    verify_code = verify_result.get("VerifyCode", "")
    passed = verify_result.get("VerifyResult", False)

    if passed:
        return jsonify({"ok": True, "verify_code": verify_code})
    else:
        return jsonify({"ok": False, "verify_code": verify_code, "error": f"captcha rejected: {verify_code}"}), 200
