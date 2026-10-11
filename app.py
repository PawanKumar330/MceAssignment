import os
import sys
import uuid
import time
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, jsonify, abort, make_response
)
from werkzeug.security import generate_password_hash, check_password_hash

# Load environment variables
load_dotenv()

from sheets_service import sheets_service
from auth import (
    admin_required, faculty_required, login_required,
    generate_csrf_token, validate_csrf_token,
    is_ip_locked, record_failed_login, clear_failed_login, get_client_ip
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "fallback-secret-key-change-in-prod-3914810")

# Session cookie security settings
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production"
)

# Configuration from Environment
MASTER_SHEET_ID = os.environ.get("MASTER_SHEET_ID", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()

# Inject helper functions into Jinja templates
@app.context_processor
def inject_globals():
    return {
        "csrf_token": generate_csrf_token,
        "service_account_email": sheets_service.service_account_email,
        "is_sheets_configured": sheets_service.is_configured(),
        "now_year": datetime.now().year
    }

# CSRF check before modifying requests
@app.before_request
def csrf_protect():
    if request.method in ["POST", "PUT", "DELETE", "PATCH"]:
        # Allow checking header X-CSRF-Token or form field csrf_token
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not validate_csrf_token(token):
            if request.is_json:
                return jsonify({"success": False, "error": "Invalid or missing CSRF token. Please refresh the page."}), 400
            flash("Security check failed: Invalid CSRF token. Please try again.", "error")
            return redirect(request.referrer or url_for("login"))


# ==========================================
# AUTHENTICATION ROUTES
# ==========================================

@app.route("/")
def index():
    if session.get("logged_in"):
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        elif session.get("role") == "faculty":
            return redirect(url_for("faculty_panel"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))

    ip = get_client_ip()
    locked, remaining = is_ip_locked(ip)
    if locked:
        return render_template(
            "login.html",
            error=f"Too many failed login attempts. This IP is locked. Please try again in {remaining} seconds."
        ), 429

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            return render_template("login.html", error="Please provide both username and password."), 400

        # Check Master Admin Login
        if username.lower() == ADMIN_USERNAME.lower():
            if ADMIN_PASSWORD_HASH and check_password_hash(ADMIN_PASSWORD_HASH, password):
                clear_failed_login(ip)
                session.clear()
                session["logged_in"] = True
                session["role"] = "admin"
                session["username"] = ADMIN_USERNAME
                session["user_name"] = "Master Admin"

                # Log audit
                if MASTER_SHEET_ID:
                    sheets_service.log_audit(MASTER_SHEET_ID, "Master Admin", "Login", "System", "Admin logged in")

                flash("Welcome back, Master Admin!", "success")
                next_page = request.args.get("next")
                return redirect(next_page if next_page and next_page.startswith("/") else url_for("admin_dashboard"))
            else:
                record_failed_login(ip)
                return render_template("login.html", error="Invalid admin username or password."), 401

        # Check Faculty Login in Master Sheet
        if not MASTER_SHEET_ID:
            record_failed_login(ip)
            return render_template("login.html", error="System error: Master Sheet is not configured by Admin."), 500

        faculty_list = sheets_service.get_all_faculty(MASTER_SHEET_ID, use_cache=False)
        matched_faculty = None
        for f in faculty_list:
            if f.get("username", "").strip().lower() == username.lower():
                matched_faculty = f
                break

        if matched_faculty:
            # Check if active
            if not matched_faculty.get("active", True):
                return render_template("login.html", error="Your faculty account is deactivated. Please contact the administrator."), 403

            pw_hash = matched_faculty.get("password_hash", "")
            if pw_hash and check_password_hash(pw_hash, password):
                clear_failed_login(ip)
                session.clear()
                session["logged_in"] = True
                session["role"] = "faculty"
                session["faculty_id"] = matched_faculty.get("faculty_id")
                session["username"] = matched_faculty.get("username")
                session["user_name"] = matched_faculty.get("name")
                session["subject"] = matched_faculty.get("subject")
                session["sheet_id"] = matched_faculty.get("sheet_id")
                session["worksheet_name"] = matched_faculty.get("worksheet_name", "Sheet1")

                # Update last activity in Master Sheet
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheets_service.update_faculty(
                    MASTER_SHEET_ID,
                    matched_faculty.get("faculty_id"),
                    {"last_activity": now_str}
                )
                sheets_service.log_audit(
                    MASTER_SHEET_ID,
                    matched_faculty.get("name"),
                    "Login",
                    matched_faculty.get("subject"),
                    "Faculty logged in"
                )

                flash(f"Welcome back, {matched_faculty.get('name')}!", "success")
                next_page = request.args.get("next")
                return redirect(next_page if next_page and next_page.startswith("/") else url_for("faculty_panel"))

        record_failed_login(ip)
        return render_template("login.html", error="Invalid username or password."), 401

    return render_template("login.html")

@app.route("/logout")
def logout():
    who = session.get("user_name", "User")
    role = session.get("role", "")
    subj = session.get("subject", "System")
    if MASTER_SHEET_ID and session.get("logged_in"):
        sheets_service.log_audit(MASTER_SHEET_ID, who, "Logout", subj, f"{role} logged out")

    session.clear()
    flash("You have been securely logged out.", "info")
    return redirect(url_for("login"))


# ==========================================
# MASTER ADMIN ROUTES
# ==========================================

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    if not MASTER_SHEET_ID:
        flash("MASTER_SHEET_ID environment variable is not configured. Please set it in your environment.", "warning")
        return render_template("admin_dashboard.html", faculty_list=[], stats={"total": 0, "active": 0, "subjects": 0})

    # Ensure sheets are initialized
    sheets_service.init_master_sheet(MASTER_SHEET_ID)
    faculty_list = sheets_service.get_all_faculty(MASTER_SHEET_ID)

    total = len(faculty_list)
    active_count = sum(1 for f in faculty_list if f.get("active", True))
    subjects_count = len(set(f.get("subject", "") for f in faculty_list if f.get("subject")))

    return render_template(
        "admin_dashboard.html",
        faculty_list=faculty_list,
        stats={
            "total": total,
            "active": active_count,
            "inactive": total - active_count,
            "subjects": subjects_count
        }
    )

@app.route("/admin/overview")
@admin_required
def admin_overview():
    """
    Overview page: per subject, count of submitted vs not submitted and a progress bar.
    """
    if not MASTER_SHEET_ID:
        flash("MASTER_SHEET_ID is not configured.", "warning")
        return render_template("admin_overview.html", subjects_data=[])

    faculty_list = sheets_service.get_all_faculty(MASTER_SHEET_ID)
    subjects_data = []

    for f in faculty_list:
        subj_name = f.get("subject", "Unnamed Subject")
        sheet_id = f.get("sheet_id", "")
        ws_name = f.get("worksheet_name", "")

        # Fetch subject sheet submission statistics
        stats = {
            "faculty_name": f.get("name"),
            "subject": subj_name,
            "sheet_id": sheet_id,
            "worksheet_name": ws_name,
            "total_students": 0,
            "submitted": 0,
            "pending": 0,
            "percentage": 0,
            "assignments_count": 0,
            "status": "connected"
        }

        if sheet_id:
            data = sheets_service.get_subject_sheet_data(sheet_id, ws_name, use_cache=True)
            if data.get("error"):
                stats["status"] = "error"
                stats["error_msg"] = data.get("error")
            else:
                students = data.get("students", [])
                assignments = data.get("assignments", [])
                stats["total_students"] = len(students)
                stats["assignments_count"] = len(assignments)

                if assignments and students:
                    # Look at the latest or primary assignment for progress metric
                    primary_asg = assignments[0]["name"]
                    stats["primary_assignment"] = primary_asg
                    sub_count = 0
                    for s in students:
                        rec = s.get("assignments", {}).get(primary_asg, {})
                        if rec.get("status") == "Submitted":
                            sub_count += 1
                    stats["submitted"] = sub_count
                    stats["pending"] = len(students) - sub_count
                    stats["percentage"] = round((sub_count / len(students)) * 100, 1) if students else 0
                else:
                    stats["primary_assignment"] = "None"
        else:
            stats["status"] = "no_sheet"

        subjects_data.append(stats)

    return render_template("admin_overview.html", subjects_data=subjects_data)

@app.route("/admin/faculty/add", methods=["POST"])
@admin_required
def admin_add_faculty():
    name = request.form.get("name", "").strip()
    subject = request.form.get("subject", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    auto_generate = request.form.get("auto_generate_password") == "true"
    sheet_url_or_id = request.form.get("sheet_id", "").strip()
    worksheet_name = request.form.get("worksheet_name", "Sheet1").strip()

    if not name or not subject or not username:
        return jsonify({"success": False, "error": "Name, Subject, and Username are required."}), 400

    # Auto-generate password if requested or if password is empty
    plain_password = password
    if auto_generate or not plain_password:
        plain_password = secrets.token_urlsafe(8)

    pw_hash = generate_password_hash(plain_password)
    clean_sheet_id = sheets_service.extract_sheet_id(sheet_url_or_id)

    # Check for duplicate username in config
    existing = sheets_service.get_all_faculty(MASTER_SHEET_ID, use_cache=False)
    for f in existing:
        if f.get("username", "").lower() == username.lower():
            return jsonify({"success": False, "error": f"Username '{username}' is already taken."}), 400

    faculty_id = f"fac_{uuid.uuid4().hex[:8]}"
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    faculty_record = {
        "faculty_id": faculty_id,
        "name": name,
        "subject": subject,
        "username": username,
        "password_hash": pw_hash,
        "sheet_id": clean_sheet_id,
        "worksheet_name": worksheet_name or "Sheet1",
        "active": True,
        "created_at": created_at,
        "last_activity": "Never"
    }

    success = sheets_service.add_faculty(MASTER_SHEET_ID, faculty_record)
    if success:
        sheets_service.log_audit(
            MASTER_SHEET_ID, "Master Admin", "Add Faculty", subject,
            f"Created faculty {name} ({username})"
        )
        return jsonify({
            "success": True,
            "message": "Faculty added successfully.",
            "generated_password": plain_password,
            "faculty": faculty_record
        })
    else:
        return jsonify({"success": False, "error": "Failed to write faculty record to Google Sheet."}), 500

@app.route("/admin/faculty/edit", methods=["POST"])
@admin_required
def admin_edit_faculty():
    faculty_id = request.form.get("faculty_id", "").strip()
    name = request.form.get("name", "").strip()
    subject = request.form.get("subject", "").strip()
    username = request.form.get("username", "").strip()
    sheet_url_or_id = request.form.get("sheet_id", "").strip()
    worksheet_name = request.form.get("worksheet_name", "Sheet1").strip()

    if not faculty_id or not name or not subject or not username:
        return jsonify({"success": False, "error": "Missing required fields."}), 400

    update_fields = {
        "name": name,
        "subject": subject,
        "username": username,
        "sheet_id": sheets_service.extract_sheet_id(sheet_url_or_id),
        "worksheet_name": worksheet_name
    }

    success = sheets_service.update_faculty(MASTER_SHEET_ID, faculty_id, update_fields)
    if success:
        sheets_service.log_audit(MASTER_SHEET_ID, "Master Admin", "Edit Faculty", subject, f"Updated faculty {faculty_id}")
        return jsonify({"success": True, "message": "Faculty profile updated successfully."})
    return jsonify({"success": False, "error": "Failed to update faculty in sheet."}), 500

@app.route("/admin/faculty/reset-password", methods=["POST"])
@admin_required
def admin_reset_password():
    faculty_id = request.form.get("faculty_id", "").strip()
    new_password = request.form.get("new_password", "").strip()
    auto_generate = request.form.get("auto_generate") == "true"

    if not faculty_id:
        return jsonify({"success": False, "error": "Faculty ID is required."}), 400

    plain_password = new_password
    if auto_generate or not plain_password:
        plain_password = secrets.token_urlsafe(8)

    pw_hash = generate_password_hash(plain_password)
    success = sheets_service.update_faculty(MASTER_SHEET_ID, faculty_id, {"password_hash": pw_hash})

    if success:
        sheets_service.log_audit(MASTER_SHEET_ID, "Master Admin", "Reset Password", "Faculty Auth", f"Reset password for {faculty_id}")
        return jsonify({
            "success": True,
            "message": "Password reset successfully.",
            "new_password": plain_password
        })
    return jsonify({"success": False, "error": "Failed to reset password."}), 500

@app.route("/admin/faculty/toggle-active", methods=["POST"])
@admin_required
def admin_toggle_active():
    faculty_id = request.form.get("faculty_id", "").strip()
    active_str = request.form.get("active", "true").lower()
    active = active_str in ["true", "1", "yes"]

    if not faculty_id:
        return jsonify({"success": False, "error": "Faculty ID is required."}), 400

    success = sheets_service.update_faculty(MASTER_SHEET_ID, faculty_id, {"active": active})
    if success:
        status_label = "activated" if active else "deactivated"
        sheets_service.log_audit(MASTER_SHEET_ID, "Master Admin", "Toggle Status", "Faculty", f"Faculty {faculty_id} {status_label}")
        return jsonify({"success": True, "active": active, "message": f"Faculty successfully {status_label}."})
    return jsonify({"success": False, "error": "Failed to update status."}), 500

@app.route("/admin/faculty/delete", methods=["POST"])
@admin_required
def admin_delete_faculty():
    faculty_id = request.form.get("faculty_id", "").strip()
    if not faculty_id:
        return jsonify({"success": False, "error": "Faculty ID is required."}), 400

    success = sheets_service.delete_faculty(MASTER_SHEET_ID, faculty_id)
    if success:
        sheets_service.log_audit(MASTER_SHEET_ID, "Master Admin", "Delete Faculty", "Faculty", f"Deleted faculty {faculty_id}")
        return jsonify({"success": True, "message": "Faculty member deleted successfully."})
    return jsonify({"success": False, "error": "Failed to delete faculty from sheet."}), 500

@app.route("/admin/test-connection", methods=["POST"])
@admin_required
def admin_test_connection():
    sheet_url_or_id = request.form.get("sheet_id", "").strip()
    worksheet_name = request.form.get("worksheet_name", "").strip()

    if not sheet_url_or_id:
        return jsonify({"success": False, "error": "Sheet URL or ID is required."}), 400

    success, message = sheets_service.test_connection(sheet_url_or_id, worksheet_name)
    return jsonify({
        "success": success,
        "message": message,
        "service_account_email": sheets_service.service_account_email
    })


# ==========================================
# FACULTY PANEL ROUTES
# ==========================================

@app.route("/faculty")
@faculty_required
def faculty_panel():
    faculty_id = session.get("faculty_id")
    subject = session.get("subject", "Assigned Subject")
    sheet_id = session.get("sheet_id", "")
    worksheet_name = session.get("worksheet_name", "Sheet1")
    faculty_name = session.get("user_name", "Faculty")

    if not sheet_id:
        return render_template(
            "faculty_panel.html",
            error_message="No Google Sheet has been linked to your account. Please contact the Master Admin.",
            faculty_name=faculty_name,
            subject=subject,
            students=[],
            assignments=[]
        )

    # Load sheet data
    data = sheets_service.get_subject_sheet_data(sheet_id, worksheet_name, use_cache=True)
    if data.get("error"):
        return render_template(
            "faculty_panel.html",
            error_message=f"Error accessing linked sheet: {data.get('error')}",
            faculty_name=faculty_name,
            subject=subject,
            sheet_id=sheet_id,
            worksheet_name=worksheet_name,
            students=[],
            assignments=[]
        )

    students = data.get("students", [])
    assignments = data.get("assignments", [])

    # If no assignments found, we can suggest default "Assignment 1"
    if not assignments:
        assignments = [{"name": "Assignment 1", "status_col": None, "date_col": None}]

    return render_template(
        "faculty_panel.html",
        faculty_name=faculty_name,
        subject=subject,
        sheet_id=sheet_id,
        worksheet_name=worksheet_name,
        spreadsheet_title=data.get("spreadsheet_title", "Google Sheet"),
        students=students,
        assignments=assignments
    )

@app.route("/faculty/create-assignment", methods=["POST"])
@faculty_required
def faculty_create_assignment():
    assignment_name = request.form.get("assignment_name", "").strip()
    sheet_id = session.get("sheet_id")
    worksheet_name = session.get("worksheet_name", "Sheet1")

    if not assignment_name:
        return jsonify({"success": False, "error": "Assignment name cannot be empty."}), 400

    if not sheet_id:
        return jsonify({"success": False, "error": "No sheet linked to your account."}), 400

    result = sheets_service.ensure_assignment_columns(sheet_id, worksheet_name, assignment_name)
    if result.get("success"):
        sheets_service.log_audit(
            MASTER_SHEET_ID, session.get("user_name"), "Create Assignment",
            session.get("subject"), f"Added columns for '{assignment_name}'"
        )
        return jsonify({"success": True, "assignment_name": assignment_name})
    return jsonify({"success": False, "error": result.get("error")}), 500

@app.route("/faculty/save-submissions", methods=["POST"])
@faculty_required
def faculty_save_submissions():
    """
    Saves submission status and date batch updates for an assignment.
    """
    sheet_id = session.get("sheet_id")
    worksheet_name = session.get("worksheet_name", "Sheet1")
    assignment_name = request.form.get("assignment_name", "").strip()
    updates_json = request.form.get("updates", "[]")

    if not sheet_id:
        return jsonify({"success": False, "error": "No sheet linked to account."}), 400
    if not assignment_name:
        return jsonify({"success": False, "error": "Assignment name is required."}), 400

    import json
    try:
        updates = json.loads(updates_json)
    except Exception as e:
        return jsonify({"success": False, "error": f"Invalid updates payload: {e}"}), 400

    success, message = sheets_service.save_submissions_batch(
        sheet_id, worksheet_name, assignment_name, updates
    )

    if success:
        sheets_service.log_audit(
            MASTER_SHEET_ID, session.get("user_name"), "Batch Save Submissions",
            session.get("subject"), f"Saved {len(updates)} records for {assignment_name}"
        )
        return jsonify({"success": True, "message": message, "saved_count": len(updates)})
    return jsonify({"success": False, "error": message}), 500


# ==========================================
# HEALTH & ERROR HANDLERS
# ==========================================

@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sheets_configured": sheets_service.is_configured(),
        "service_account": sheets_service.service_account_email
    })

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, title="Forbidden", message=str(e)), 403

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, title="Page Not Found", message="The requested resource could not be found."), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Internal Server Error: {e}")
    return render_template("error.html", code=500, title="Server Error", message="An unexpected error occurred. Please try again later."), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") != "production")
