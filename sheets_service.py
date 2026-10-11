import os
import json
import time
import logging
from functools import wraps
from typing import Dict, Any, List, Optional, Tuple
import gspread
from google.oauth2.service_account import Credentials
from google.auth.exceptions import GoogleAuthError

logger = logging.getLogger(__name__)

# Required Google API Scopes for Sheets and Drive
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Simple in-memory cache to briefly cache sheet reads (5-10 seconds)
# Format: cache_key -> (timestamp, data)
_CACHE = {}
CACHE_TTL_SECONDS = 8

def with_retry_backoff(max_retries=3, initial_delay=1.0, factor=2.0):
    """Decorator to retry Google Sheets API calls on rate limits or transient errors."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except gspread.exceptions.APIError as e:
                    last_exception = e
                    # Check for rate limit (429) or server errors (500, 503)
                    code = getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None
                    logger.warning(f"Google API Error (attempt {attempt}/{max_retries}, code {code}): {e}")
                    if attempt == max_retries:
                        raise
                    time.sleep(delay)
                    delay *= factor
                except Exception as e:
                    last_exception = e
                    logger.warning(f"Transient error in {func.__name__} (attempt {attempt}/{max_retries}): {e}")
                    if attempt == max_retries:
                        raise
                    time.sleep(delay)
                    delay *= factor
            if last_exception:
                raise last_exception
        return wrapper
    return decorator


class SheetsService:
    def __init__(self):
        self._client: Optional[gspread.Client] = None
        self._service_account_email: Optional[str] = None
        self._init_client()

    def _init_client(self):
        """Initializes the gspread client from GOOGLE_SERVICE_ACCOUNT_JSON env var or file."""
        sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not sa_json:
            # Fallback check for service_account.json file path
            sa_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
            if os.path.exists(sa_file):
                try:
                    with open(sa_file, "r", encoding="utf-8") as f:
                        sa_json = f.read()
                except Exception as e:
                    logger.error(f"Failed to read service account file {sa_file}: {e}")

        if not sa_json:
            logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set or service_account.json missing.")
            return

        try:
            # Parse json if string
            if isinstance(sa_json, str):
                info = json.loads(sa_json.strip())
            else:
                info = sa_json

            self._service_account_email = info.get("client_email")
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
            self._client = gspread.authorize(creds)
            logger.info(f"Google Sheets Service initialized for: {self._service_account_email}")
        except Exception as e:
            logger.error(f"Error initializing Google Sheets client: {e}")
            self._client = None

    @property
    def service_account_email(self) -> str:
        """Returns the client email of the active service account."""
        if self._service_account_email:
            return self._service_account_email
        # Try inspecting sa_json
        sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if sa_json:
            try:
                info = json.loads(sa_json)
                self._service_account_email = info.get("client_email")
                return self._service_account_email or "Not found in JSON"
            except Exception:
                pass
        return "Not Configured"

    def is_configured(self) -> bool:
        return self._client is not None

    def extract_sheet_id(self, url_or_id: str) -> str:
        """Extracts sheet ID from full Google Spreadsheet URL or returns clean ID."""
        if not url_or_id:
            return ""
        s = url_or_id.strip()
        if "/d/" in s:
            # e.g. https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit...
            parts = s.split("/d/")
            if len(parts) > 1:
                return parts[1].split("/")[0]
        return s

    @with_retry_backoff()
    def test_connection(self, sheet_url_or_id: str, worksheet_name: str = "") -> Tuple[bool, str]:
        """
        Tests whether the service account can open and read/write the given spreadsheet.
        Returns (success: bool, message: str).
        """
        if not self.is_configured():
            self._init_client()
            if not self.is_configured():
                return False, f"Service Account is not configured. Please set GOOGLE_SERVICE_ACCOUNT_JSON."

        sheet_id = self.extract_sheet_id(sheet_url_or_id)
        if not sheet_id:
            return False, "Google Sheet ID or URL is empty."

        try:
            sh = self._client.open_by_key(sheet_id)
            # Try to get worksheet
            if worksheet_name and worksheet_name.strip():
                try:
                    ws = sh.worksheet(worksheet_name.strip())
                except gspread.WorksheetNotFound:
                    available = [w.title for w in sh.worksheets()]
                    return False, f"Worksheet '{worksheet_name}' not found. Available sheets: {', '.join(available)}"
            else:
                ws = sh.get_worksheet(0)
                if not ws:
                    return False, "Spreadsheet has no worksheets."

            # Read a sample cell or header to verify read permission
            _ = ws.row_values(1)
            return True, f"Connection successful! Verified access to '{sh.title}' ({ws.title})."

        except gspread.exceptions.SpreadsheetNotFound:
            return False, (
                f"Spreadsheet not found or access denied. Ensure you shared the spreadsheet with "
                f"Editor permission to: {self.service_account_email}"
            )
        except gspread.exceptions.APIError as e:
            code = getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None
            if code == 403 or "permission" in str(e).lower():
                return False, (
                    f"Permission Denied (403). Please share your Google Sheet with Editor permission to: "
                    f"{self.service_account_email}"
                )
            return False, f"Google API Error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error testing connection: {str(e)}"

    # ==========================================
    # MASTER SHEET: CONFIG & AUDIT LOG
    # ==========================================

    @with_retry_backoff()
    def init_master_sheet(self, master_sheet_id: str):
        """
        Ensures 'Faculty_Config' and 'Audit_Log' worksheets exist in the master sheet
        with appropriate headers.
        """
        sheet_id = self.extract_sheet_id(master_sheet_id)
        if not sheet_id or not self._client:
            return

        try:
            sh = self._client.open_by_key(sheet_id)
            titles = [w.title for w in sh.worksheets()]

            # 1. Faculty_Config
            if "Faculty_Config" not in titles:
                ws_config = sh.add_worksheet(title="Faculty_Config", rows=100, cols=10)
                ws_config.append_row([
                    "faculty_id", "name", "subject", "username", "password_hash",
                    "sheet_id", "worksheet_name", "active", "created_at", "last_activity"
                ])
                logger.info("Created Faculty_Config worksheet in Master Sheet.")
            else:
                # Ensure header is present
                ws_config = sh.worksheet("Faculty_Config")
                rows = ws_config.row_values(1)
                if not rows:
                    ws_config.append_row([
                        "faculty_id", "name", "subject", "username", "password_hash",
                        "sheet_id", "worksheet_name", "active", "created_at", "last_activity"
                    ])

            # 2. Audit_Log
            if "Audit_Log" not in titles:
                ws_audit = sh.add_worksheet(title="Audit_Log", rows=500, cols=5)
                ws_audit.append_row(["timestamp", "who", "action", "subject", "details"])
                logger.info("Created Audit_Log worksheet in Master Sheet.")
        except Exception as e:
            logger.error(f"Error initializing master sheet tabs: {e}")

    @with_retry_backoff()
    def get_all_faculty(self, master_sheet_id: str, use_cache: bool = True) -> List[Dict[str, Any]]:
        """Reads all faculty records from Faculty_Config tab in Master Sheet."""
        sheet_id = self.extract_sheet_id(master_sheet_id)
        if not sheet_id or not self.is_configured():
            return []

        cache_key = f"faculty_config_{sheet_id}"
        now = time.time()
        if use_cache and cache_key in _CACHE:
            ts, val = _CACHE[cache_key]
            if now - ts < CACHE_TTL_SECONDS:
                return val

        try:
            sh = self._client.open_by_key(sheet_id)
            ws = sh.worksheet("Faculty_Config")
            records = ws.get_all_records()
            # Normalize active to boolean and clean fields
            faculty_list = []
            for r in records:
                # Handle boolean active
                raw_active = str(r.get("active", "TRUE")).strip().upper()
                active = raw_active in ["TRUE", "1", "YES"]
                faculty_list.append({
                    "faculty_id": str(r.get("faculty_id", "")),
                    "name": str(r.get("name", "")),
                    "subject": str(r.get("subject", "")),
                    "username": str(r.get("username", "")),
                    "password_hash": str(r.get("password_hash", "")),
                    "sheet_id": str(r.get("sheet_id", "")),
                    "worksheet_name": str(r.get("worksheet_name", "")),
                    "active": active,
                    "created_at": str(r.get("created_at", "")),
                    "last_activity": str(r.get("last_activity", ""))
                })
            _CACHE[cache_key] = (now, faculty_list)
            return faculty_list
        except Exception as e:
            logger.error(f"Error fetching faculty configs: {e}")
            return []

    def invalidate_cache(self, key_prefix: str = ""):
        """Invalidates cached sheet data."""
        global _CACHE
        if not key_prefix:
            _CACHE.clear()
        else:
            keys_to_del = [k for k in _CACHE.keys() if k.startswith(key_prefix)]
            for k in keys_to_del:
                _CACHE.pop(k, None)

    @with_retry_backoff()
    def add_faculty(self, master_sheet_id: str, faculty_data: Dict[str, Any]) -> bool:
        """Appends a new faculty row to Faculty_Config."""
        sheet_id = self.extract_sheet_id(master_sheet_id)
        if not sheet_id or not self.is_configured():
            return False

        try:
            sh = self._client.open_by_key(sheet_id)
            ws = sh.worksheet("Faculty_Config")
            row = [
                faculty_data.get("faculty_id", ""),
                faculty_data.get("name", ""),
                faculty_data.get("subject", ""),
                faculty_data.get("username", ""),
                faculty_data.get("password_hash", ""),
                self.extract_sheet_id(faculty_data.get("sheet_id", "")),
                faculty_data.get("worksheet_name", ""),
                "TRUE" if faculty_data.get("active", True) else "FALSE",
                faculty_data.get("created_at", ""),
                faculty_data.get("last_activity", "")
            ]
            ws.append_row(row)
            self.invalidate_cache(f"faculty_config_{sheet_id}")
            return True
        except Exception as e:
            logger.error(f"Error appending faculty: {e}")
            return False

    @with_retry_backoff()
    def update_faculty(self, master_sheet_id: str, faculty_id: str, update_fields: Dict[str, Any]) -> bool:
        """Updates specific fields for a faculty member in Faculty_Config."""
        sheet_id = self.extract_sheet_id(master_sheet_id)
        if not sheet_id or not self.is_configured():
            return False

        try:
            sh = self._client.open_by_key(sheet_id)
            ws = sh.worksheet("Faculty_Config")
            all_values = ws.get_all_values()
            if not all_values:
                return False

            headers = all_values[0]
            header_map = {h: idx for idx, h in enumerate(headers)}

            row_idx_to_update = None
            for idx, r in enumerate(all_values[1:], start=2):
                if len(r) > 0 and r[0] == faculty_id:
                    row_idx_to_update = idx
                    break

            if not row_idx_to_update:
                logger.warning(f"Faculty ID {faculty_id} not found in sheet for update.")
                return False

            updates = []
            for field, val in update_fields.items():
                if field in header_map:
                    col_idx = header_map[field] + 1
                    formatted_val = val
                    if field == "active":
                        formatted_val = "TRUE" if val else "FALSE"
                    elif field == "sheet_id":
                        formatted_val = self.extract_sheet_id(str(val))
                    updates.append({
                        'range': gspread.utils.rowcol_to_a1(row_idx_to_update, col_idx),
                        'values': [[str(formatted_val)]]
                    })

            if updates:
                ws.batch_update(updates)
                self.invalidate_cache(f"faculty_config_{sheet_id}")
            return True
        except Exception as e:
            logger.error(f"Error updating faculty {faculty_id}: {e}")
            return False

    @with_retry_backoff()
    def delete_faculty(self, master_sheet_id: str, faculty_id: str) -> bool:
        """Deletes a faculty member row from Faculty_Config."""
        sheet_id = self.extract_sheet_id(master_sheet_id)
        if not sheet_id or not self.is_configured():
            return False

        try:
            sh = self._client.open_by_key(sheet_id)
            ws = sh.worksheet("Faculty_Config")
            all_values = ws.get_all_values()
            for idx, r in enumerate(all_values[1:], start=2):
                if len(r) > 0 and r[0] == faculty_id:
                    ws.delete_rows(idx)
                    self.invalidate_cache(f"faculty_config_{sheet_id}")
                    return True
            return False
        except Exception as e:
            logger.error(f"Error deleting faculty {faculty_id}: {e}")
            return False

    @with_retry_backoff()
    def log_audit(self, master_sheet_id: str, who: str, action: str, subject: str, details: str = ""):
        """Appends an audit log entry to Audit_Log worksheet."""
        sheet_id = self.extract_sheet_id(master_sheet_id)
        if not sheet_id or not self.is_configured():
            return

        try:
            sh = self._client.open_by_key(sheet_id)
            ws = sh.worksheet("Audit_Log")
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            ws.append_row([timestamp, who, action, subject, details])
        except Exception as e:
            logger.error(f"Failed to append audit log: {e}")

    # ==========================================
    # FACULTY SUBJECT SHEET: STUDENTS & SUBMISSIONS
    # ==========================================

    @with_retry_backoff()
    def get_subject_sheet_data(self, sheet_url_or_id: str, worksheet_name: str = "", use_cache: bool = True) -> Dict[str, Any]:
        """
        Loads the subject sheet data:
        - Headers (Row 1)
        - Discovers existing assignments (pairs of Status & Date columns)
        - List of students with roll_no, name, row_index, and assignment statuses/dates.
        """
        sheet_id = self.extract_sheet_id(sheet_url_or_id)
        if not sheet_id or not self.is_configured():
            return {"error": "Service account or Sheet ID not configured.", "headers": [], "assignments": [], "students": []}

        cache_key = f"subject_{sheet_id}_{worksheet_name}"
        now = time.time()
        if use_cache and cache_key in _CACHE:
            ts, val = _CACHE[cache_key]
            if now - ts < CACHE_TTL_SECONDS:
                return val

        try:
            sh = self._client.open_by_key(sheet_id)
            if worksheet_name and worksheet_name.strip():
                ws = sh.worksheet(worksheet_name.strip())
            else:
                ws = sh.get_worksheet(0)

            all_values = ws.get_all_values()
            if not all_values:
                return {"headers": [], "assignments": [], "students": [], "title": ws.title}

            headers = all_values[0]
            # Identify key student identifier columns dynamically
            reg_col_idx = None
            roll_col_idx = None
            name_col_idx = None

            for col_idx, h in enumerate(headers):
                h_norm = h.strip().lower()
                if h_norm in ["registration number", "reg number", "reg no", "registration no", "reg_no", "reg"]:
                    reg_col_idx = col_idx
                elif h_norm in ["roll number", "roll no", "roll_no", "roll"]:
                    roll_col_idx = col_idx
                elif h_norm in ["student name", "name", "full name", "student_name"]:
                    name_col_idx = col_idx

            # Fallbacks if columns aren't standard names
            if roll_col_idx is None:
                roll_col_idx = 1 if (reg_col_idx == 0 and len(headers) > 1) else 0
            if name_col_idx is None:
                name_col_idx = 2 if (roll_col_idx == 1 and reg_col_idx == 0) else (1 if roll_col_idx == 0 else 1)

            # Detect Assignments
            assignments = []
            assigned_map = {}

            # First pass: find multi-assignment columns e.g. "Assignment X Status", "Assignment X Date"
            for col_idx, h in enumerate(headers):
                h_clean = h.strip()
                h_lower = h_clean.lower()
                if h_lower.endswith(" status") and len(h_clean) > 7:
                    asg_name = h_clean[:-7].strip()
                    if asg_name not in assigned_map:
                        assigned_map[asg_name] = {"status_col": col_idx, "date_col": None, "status_header": h_clean, "date_header": None}
                    else:
                        assigned_map[asg_name]["status_col"] = col_idx
                        assigned_map[asg_name]["status_header"] = h_clean
                elif (h_lower.endswith(" date") or h_lower.endswith(" date of submission")) and not (h_lower in ["date", "date of submission", "submission date"]):
                    if h_lower.endswith(" date of submission"):
                        asg_name = h_clean[:-19].strip()
                    else:
                        asg_name = h_clean[:-5].strip()
                    if asg_name not in assigned_map:
                        assigned_map[asg_name] = {"status_col": None, "date_col": col_idx, "status_header": None, "date_header": h_clean}
                    else:
                        assigned_map[asg_name]["date_col"] = col_idx
                        assigned_map[asg_name]["date_header"] = h_clean

            # Build list of valid multi-assignments
            for name, cols in assigned_map.items():
                if cols["status_col"] is not None:
                    assignments.append({
                        "name": name,
                        "status_col": cols["status_col"],
                        "date_col": cols["date_col"],
                        "status_header": cols.get("status_header") or f"{name} Status",
                        "date_header": cols.get("date_header") or f"{name} Date"
                    })

            # Check if there is a standalone generic "Status" and "Date Of Submission" / "Date"
            if not assignments:
                status_idx = None
                date_idx = None
                status_hdr = "Status"
                date_hdr = "Date Of Submission"
                for col_idx, h in enumerate(headers):
                    h_norm = h.strip().lower()
                    if h_norm == "status":
                        status_idx = col_idx
                        status_hdr = h.strip()
                    elif h_norm in ["date of submission", "submission date", "submission_date", "date"]:
                        date_idx = col_idx
                        date_hdr = h.strip()

                if status_idx is not None:
                    assignments.append({
                        "name": "Assignment 1",
                        "status_col": status_idx,
                        "date_col": date_idx,
                        "status_header": status_hdr,
                        "date_header": date_hdr
                    })

            students = []
            for row_idx, row in enumerate(all_values[1:], start=2):
                roll_no = row[roll_col_idx].strip() if len(row) > roll_col_idx else ""
                name = row[name_col_idx].strip() if len(row) > name_col_idx else ""
                reg_no = row[reg_col_idx].strip() if (reg_col_idx is not None and len(row) > reg_col_idx) else ""
                
                if not roll_no and not name and not reg_no:
                    continue  # Skip blank rows

                assignment_records = {}
                for asg in assignments:
                    s_col = asg["status_col"]
                    d_col = asg["date_col"]
                    status_val = row[s_col].strip() if s_col is not None and len(row) > s_col else "Not Submitted"
                    date_val = row[d_col].strip() if d_col is not None and len(row) > d_col else ""
                    # Normalize status
                    if status_val.lower() in ["submitted", "done", "yes", "completed"]:
                        status_val = "Submitted"
                    else:
                        status_val = "Not Submitted"

                    assignment_records[asg["name"]] = {
                        "status": status_val,
                        "date": date_val
                    }

                students.append({
                    "row_index": row_idx,
                    "roll_no": roll_no,
                    "registration_no": reg_no,
                    "name": name,
                    "assignments": assignment_records
                })

            result = {
                "spreadsheet_title": sh.title,
                "worksheet_title": ws.title,
                "headers": headers,
                "assignments": assignments,
                "students": students,
                "total_rows": len(all_values)
            }
            _CACHE[cache_key] = (now, result)
            return result

        except Exception as e:
            logger.error(f"Error fetching subject sheet data for {sheet_id}: {e}")
            return {"error": str(e), "headers": [], "assignments": [], "students": []}

    @with_retry_backoff()
    def ensure_assignment_columns(self, sheet_url_or_id: str, worksheet_name: str, assignment_name: str) -> Dict[str, Any]:
        """
        Checks if the assignment columns (e.g. 'Assignment 1 Status', 'Assignment 1 Date') exist.
        If not, appends them to Row 1 headers automatically.
        """
        sheet_id = self.extract_sheet_id(sheet_url_or_id)
        if not sheet_id or not self.is_configured():
            return {"success": False, "error": "Not configured"}

        try:
            sh = self._client.open_by_key(sheet_id)
            ws = sh.worksheet(worksheet_name.strip()) if worksheet_name else sh.get_worksheet(0)
            headers = ws.row_values(1)

            # Ensure minimum Roll No and Student Name headers exist
            if len(headers) == 0:
                headers = ["Roll No", "Student Name"]
                ws.update("A1:B1", [headers])

            status_header = f"{assignment_name} Status"
            date_header = f"{assignment_name} Date"

            status_exists = status_header in headers
            date_exists = date_header in headers

            col_updates = []
            if not status_exists:
                headers.append(status_header)
            if not date_exists:
                headers.append(date_header)

            if not status_exists or not date_exists:
                # Update header row
                end_col_letter = gspread.utils.rowcol_to_a1(1, len(headers))
                range_str = f"A1:{end_col_letter}"
                ws.update(range_str, [headers])
                self.invalidate_cache(f"subject_{sheet_id}_{worksheet_name}")

            return {"success": True, "assignment_name": assignment_name, "headers": headers}
        except Exception as e:
            logger.error(f"Error ensuring assignment columns: {e}")
            return {"success": False, "error": str(e)}

    @with_retry_backoff()
    def save_submissions_batch(
        self,
        sheet_url_or_id: str,
        worksheet_name: str,
        assignment_name: str,
        updates: List[Dict[str, Any]]
    ) -> Tuple[bool, str]:
        """
        Writes submission updates back to the sheet using a single batch update call.
        updates list items: {'row_index': int, 'status': 'Submitted'|'Not Submitted', 'date': 'DD-MM-YYYY'}
        Never modifies Roll No or Name columns.
        """
        sheet_id = self.extract_sheet_id(sheet_url_or_id)
        if not sheet_id or not self.is_configured():
            return False, "Google Sheets Service not configured."

        if not updates:
            return True, "No updates to save."

        try:
            sh = self._client.open_by_key(sheet_id)
            ws = sh.worksheet(worksheet_name.strip()) if worksheet_name else sh.get_worksheet(0)
            headers = ws.row_values(1)

            status_col_name = f"{assignment_name} Status"
            date_col_name = f"{assignment_name} Date"

            # Check if columns exist under exact or generic names (e.g. Status / Date Of Submission)
            status_col_idx = None
            date_col_idx = None

            for idx, h in enumerate(headers):
                h_clean = h.strip()
                h_norm = h_clean.lower()
                if h_clean == status_col_name:
                    status_col_idx = idx + 1
                elif h_clean == date_col_name:
                    date_col_idx = idx + 1

            # If not found by "Assignment 1 Status", check for generic "Status" and "Date Of Submission"
            if status_col_idx is None and assignment_name.lower() in ["assignment 1", "assignment"]:
                for idx, h in enumerate(headers):
                    h_norm = h.strip().lower()
                    if h_norm == "status" and status_col_idx is None:
                        status_col_idx = idx + 1
                    elif h_norm in ["date of submission", "submission date", "date"] and date_col_idx is None:
                        date_col_idx = idx + 1

            # If still not found, create new columns
            if status_col_idx is None or date_col_idx is None:
                ensure_res = self.ensure_assignment_columns(sheet_url_or_id, worksheet_name, assignment_name)
                if not ensure_res.get("success"):
                    return False, f"Could not create columns for {assignment_name}: {ensure_res.get('error')}"
                headers = ws.row_values(1)
                status_col_idx = headers.index(status_col_name) + 1
                date_col_idx = headers.index(date_col_name) + 1

            # Prepare batch data payload
            # If status_col and date_col are contiguous, we can combine or update each cell range
            batch_data = []
            for u in updates:
                row_idx = u.get("row_index")
                status_val = u.get("status", "Not Submitted")
                date_val = u.get("date", "")

                # If status is Not Submitted, ensure date is blank
                if status_val == "Not Submitted":
                    date_val = ""

                # Format batch update range
                if date_col_idx == status_col_idx + 1:
                    # Adjacent columns: update as a 1x2 range
                    range_name = f"{gspread.utils.rowcol_to_a1(row_idx, status_col_idx)}:{gspread.utils.rowcol_to_a1(row_idx, date_col_idx)}"
                    batch_data.append({
                        'range': range_name,
                        'values': [[status_val, date_val]]
                    })
                else:
                    # Separate columns
                    batch_data.append({
                        'range': gspread.utils.rowcol_to_a1(row_idx, status_col_idx),
                        'values': [[status_val]]
                    })
                    batch_data.append({
                        'range': gspread.utils.rowcol_to_a1(row_idx, date_col_idx),
                        'values': [[date_val]]
                    })

            if batch_data:
                ws.batch_update(batch_data)
                self.invalidate_cache(f"subject_{sheet_id}_{worksheet_name}")
                logger.info(f"Batch updated {len(updates)} students for {assignment_name} in {ws.title}")

            return True, f"Successfully saved {len(updates)} records to Google Sheet."

        except Exception as e:
            logger.error(f"Error in save_submissions_batch: {e}")
            return False, f"Failed to save to Google Sheet: {str(e)}"


# Singleton instance
sheets_service = SheetsService()
