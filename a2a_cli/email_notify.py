import smtplib
import socket
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


SMTP_HOST = "localhost"
SMTP_PORT = 25
FROM_ADDR = f"patchwise@{socket.getfqdn()}"
TO_ADDR = "nandam@qti.qualcomm.com"


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

    msg = MIMEMultipart()
    msg["From"] = FROM_ADDR
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText("\n".join(body_lines), "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.sendmail(FROM_ADDR, [to_addr], msg.as_string())
        print(f"[email] Phase {phase_number} report sent to {to_addr}")
    except Exception as exc:  # pragma: no cover - best effort notification
        print(f"[email] WARNING: could not send email: {exc}")
        fallback = f"/tmp/patchwise_phase_{phase_number}_report.txt"
        with open(fallback, "w", encoding="utf-8") as handle:
            handle.write("\n".join(body_lines))
        print(f"[email] Report saved to {fallback}")
