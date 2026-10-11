# Assignment Submission Tracker

A production-ready web application built with **Python Flask**, **Vanilla JS / Tailwind CSS**, and the **Google Sheets API** via a Service Account. Designed for institutional assignment tracking and deployable directly on **Render**.

---

## 🚀 Key Features

- **Master Admin Panel**:
  - Secure credential login with environment-driven password hashing (`scrypt`/`werkzeug`).
  - Faculty management: create, edit, activate/deactivate, delete faculty, and reset passwords.
  - "Test Sheet Connection" button verifies Google Sheets read/write permissions in real-time.
  - Displays service account email for easy copy-and-paste sharing.
  - Master Sheet storage for configurations (`Faculty_Config`) and security logs (`Audit_Log`).
  - Subject Overview page with submitted vs pending progress bars.

- **Faculty Panel**:
  - Secure login: faculty access only their designated subject and Google Sheet.
  - Interactive student roster: Roll No, Name, Status dropdown, and Date picker.
  - Auto-fills today's date in `DD-MM-YYYY` when status is marked "Submitted", clears date when set to "Not Submitted".
  - Bulk actions: "Mark all as Not Submitted" and "Mark selected as Submitted".
  - Multi-assignment support: Add "Assignment 1, 2, 3...", auto-adding header columns to the sheet.
  - High-performance batch saving: writes changes to Google Sheets in a single API call.
  - One-click CSV export of student roster and status.

- **Security & Design**:
  - CSRF protection across all forms and AJAX requests.
  - Rate limiting on login routes (lockout for 5 minutes after 5 failed attempts).
  - HttpOnly and SameSite session cookies.
  - Light and Dark mode toggle with persistent preferences.
  - Styled after Google Stitch project design system (SyncAcademia).

---

## 📋 Google Cloud Project & Service Account Setup

To connect to Google Sheets, you need a Google Cloud Service Account:

### 1. Create a Google Cloud Project
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click **Select a project** > **New Project**, name it (e.g. `Assignment-Tracker`), and click **Create**.

### 2. Enable APIs
1. In the left sidebar, navigate to **APIs & Services** > **Library**.
2. Search for and enable:
   - **Google Sheets API**
   - **Google Drive API**

### 3. Create a Service Account & Download Key
1. Go to **APIs & Services** > **Credentials**.
2. Click **Create Credentials** > **Service account**.
3. Name it (e.g., `sheet-sync-bot`), click **Create and Continue**, then click **Done**.
4. In the Service Accounts list, click the newly created service account email.
5. Go to the **Keys** tab > **Add Key** > **Create new key**.
6. Select **JSON** and click **Create**. This downloads a `.json` file containing your private key.

### 4. Share Your Google Sheets with the Service Account
1. Open the downloaded JSON file and find the `"client_email"` property (e.g. `sheet-sync-bot@your-project.iam.gserviceaccount.com`).
2. Open your **Master Google Sheet** and each **Faculty Subject Sheet** in Google Drive.
3. Click the green **Share** button in the top right.
4. Add the service account email and grant **Editor** access.

---

## 📊 Google Sheets Structure

### 1. Master Sheet (`MASTER_SHEET_ID`)
The Master Sheet automatically creates and manages two tabs:
- **`Faculty_Config`**:
  `faculty_id | name | subject | username | password_hash | sheet_id | worksheet_name | active | created_at | last_activity`
- **`Audit_Log`**:
  `timestamp | who | action | subject | details`

### 2. Faculty Subject Sheets
- **Row 1 headers**: `Roll No | Student Name | Assignment 1 Status | Assignment 1 Date | ...`
- **Rows below**: Student records (e.g. `CS-101 | John Doe | Not Submitted | `).
- *Note*: The application never overwrites the Roll No or Student Name columns.

---

## 🛠 Local Setup & Development

### 1. Clone & Set Up Virtual Environment
```bash
git clone <repo-url>
cd assignment-submission-tracker

python -m venv venv
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Generate Master Admin Password Hash
Run the included seed script:
```bash
python seed_admin.py
```
Enter your desired admin password (e.g. `MySecureAdminPass123`). Copy the generated hash.

### 3. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Fill in the values in `.env`:
- `SECRET_KEY`: Any long random secret string.
- `ADMIN_USERNAME`: `admin` (or custom).
- `ADMIN_PASSWORD_HASH`: The hash generated from step 2.
- `MASTER_SHEET_ID`: The ID from your master Google Sheet URL (`https://docs.google.com/spreadsheets/d/<ID>/edit`).
- `GOOGLE_SERVICE_ACCOUNT_JSON`: The complete raw JSON string of your service account credentials file.

### 4. Run the Application
```bash
python app.py
```
Open [http://localhost:5000](http://localhost:5000) in your browser.

---

## ☁️ Deployment on Render

This repository includes `render.yaml` and `Procfile` ready for zero-config Render deployment.

### Steps:
1. Push your repository to **GitHub** or **GitLab**.
2. Log into [Render Dashboard](https://dashboard.render.com/).
3. Click **New +** > **Web Service**.
4. Connect your Git repository.
5. Settings will be auto-detected:
   - **Environment**: `Python`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --workers 2 --threads 4 --timeout 120`
6. Add the following **Environment Variables** in Render's dashboard:
   - `FLASK_ENV`: `production`
   - `SECRET_KEY`: (Generate or provide a secure key)
   - `ADMIN_USERNAME`: `admin`
   - `ADMIN_PASSWORD_HASH`: Your generated password hash from `seed_admin.py`
   - `MASTER_SHEET_ID`: Your Master Google Sheet ID
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: The full raw JSON string from your service account `.json` key file
7. Click **Deploy Web Service**.
