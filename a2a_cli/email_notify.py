from __future__ import annotations

import smtplib
import socket
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


SMTP_HOST = "localhost"
SMTP_PORT = 25
FROM_ADDR = f"patchwise@{socket.getfqdn()}"
TO_ADDR = "nandam@qti.qualcomm.com"


def send_email(
    *,
    subject: str,
    body: str,
    to_addrs: list[str],
    cc_addrs: list[str] | None = None,
    attachments: list[str] | None = None,
    override_from: str | None = None,
) -> dict[str, str | bool]:
    if not to_addrs:
        raise ValueError("to_addrs must not be empty")

    from_addr = override_from or FROM_ADDR
    cc_rows = [row.strip() for row in (cc_addrs or []) if str(row).strip()]
    recipients = [row.strip() for row in to_addrs if str(row).strip()] + cc_rows

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc_rows:
        msg["Cc"] = ", ".join(cc_rows)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    for raw_path in attachments or []:
        path = Path(raw_path)
        if not path.exists() or not path.is_file():
            continue
        part = MIMEApplication(path.read_bytes(), Name=path.name)
        part["Content-Disposition"] = f'attachment; filename="{path.name}"'
        msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.sendmail(from_addr, recipients, msg.as_string())
        print(f"[email] sent to {', '.join(recipients)}")
        return {"sent": True, "fallback": "", "error": ""}
    except Exception as exc:  # pragma: no cover - best effort notification
        fallback = "/tmp/patchwise_submission_email_fallback.txt"
        payload = [
            f"From: {from_addr}",
            f"To: {', '.join(to_addrs)}",
            f"Cc: {', '.join(cc_rows)}",
            f"Subject: {subject}",
            "",
            body,
            "",
            "Attachments:",
        ]
        for raw_path in attachments or []:
            payload.append(f"  - {raw_path}")
        Path(fallback).write_text("\n".join(payload) + "\n", encoding="utf-8")
        print(f"[email] WARNING: could not send email: {exc}")
        print(f"[email] Mail payload saved to {fallback}")
        return {"sent": False, "fallback": fallback, "error": str(exc)}


def send_phase_report(
    phase_number: int,
    phase_name: str,
    status: str,
    tests_run: int,
    tests_passed: int,
    tests_failed: int,
    details: list[str],
    smoke_output: str = "",
    edge_results: list[str] | None = None,
    override_to: str | None = None,
) -> None:
    to_addr = override_to or TO_ADDR
    subject = f"[PatchWise A2A] Phase {phase_number} — {phase_name} — {status}"
    body_lines = [
        f"PatchWise A2A — Phase {phase_number} Validation Report",
        f"{'=' * 60}",
        f"Phase   : {phase_name}",
        f"Status  : {status}",
        f"Time    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Test Summary",
        f"{'─' * 40}",
        f"Total   : {tests_run}",
        f"Passed  : {tests_passed}",
        f"Failed  : {tests_failed}",
        "",
        "Test Details",
        f"{'─' * 40}",
    ]
    for detail in details:
        body_lines.append(f"  {detail}")

    if smoke_output:
        body_lines += ["", "Smoke Test Output", "─" * 40, smoke_output]

    if edge_results:
        body_lines += ["", "Edge Case Results", "─" * 40]
        for edge in edge_results:
            body_lines.append(f"  {edge}")

    body_lines += [
        "",
        "─" * 60,
        "Sent by PatchWise A2A automated validation system",
    ]

    result = send_email(
        subject=subject,
        body="\n".join(body_lines),
        to_addrs=[to_addr],
    )
    if bool(result.get("sent")):
        print(f"[email] Phase {phase_number} report sent to {to_addr}")
        return
    fallback = result.get("fallback") or f"/tmp/patchwise_phase_{phase_number}_report.txt"
    Path(str(fallback)).write_text("\n".join(body_lines), encoding="utf-8")
    print(f"[email] Report saved to {fallback}")
