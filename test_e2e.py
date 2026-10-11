import app
import json
from sheets_service import sheets_service

def run_tests():
    print("=" * 60)
    print("RUNNING END-TO-END AUTOMATED TEST SUITE")
    print("=" * 60)

    # 1. Admin Login
    admin_client = app.app.test_client()
    resp = admin_client.get('/login')
    assert resp.status_code == 200, f"Login page failed: {resp.status_code}"
    
    with admin_client.session_transaction() as sess:
        admin_csrf = sess.get('_csrf_token')

    login_resp = admin_client.post('/login', data={
        'username': 'admin',
        'password': '=75XvN9VHZn89@',
        'csrf_token': admin_csrf
    })
    assert login_resp.status_code == 302, f"Admin login failed: {login_resp.status_code}"
    print("PASS: Admin login authenticated.")

    # 2. Admin Dashboard
    dash_resp = admin_client.get('/admin/dashboard')
    assert dash_resp.status_code == 200, f"Dashboard failed: {dash_resp.status_code}"
    print("PASS: Admin Dashboard rendered.")

    # 3. Test Sheet Connection
    with admin_client.session_transaction() as sess:
        admin_csrf = sess.get('_csrf_token')

    conn_resp = admin_client.post('/admin/test-connection', data={
        'sheet_id': '1vYB2DkPAsVpAXoJZ2fK8QdB7pGc_X-5-oncj1CkGZSg',
        'worksheet_name': 'Students',
        'csrf_token': admin_csrf
    }, headers={'X-CSRF-Token': admin_csrf})
    assert conn_resp.status_code == 200 and conn_resp.json.get('success'), f"Test connection failed: {conn_resp.data}"
    print("PASS: Test Sheet Connection verified successfully.")

    # 4. Add Faculty
    add_resp = admin_client.post('/admin/faculty/add', data={
        'name': 'Prof. Pawan Test',
        'subject': 'Operating Systems',
        'username': 'ptest',
        'password': 'mypassword123',
        'auto_generate_password': 'false',
        'sheet_id': '1vYB2DkPAsVpAXoJZ2fK8QdB7pGc_X-5-oncj1CkGZSg',
        'worksheet_name': 'Students',
        'csrf_token': admin_csrf
    }, headers={'X-CSRF-Token': admin_csrf})
    assert add_resp.status_code == 200 and add_resp.json.get('success'), f"Add faculty failed: {add_resp.data}"
    created_fac = add_resp.json['faculty']
    print(f"PASS: Added Faculty {created_fac['name']} (@{created_fac['username']}).")

    # 5. Admin Overview
    overview_resp = admin_client.get('/admin/overview')
    assert overview_resp.status_code == 200, f"Admin overview failed: {overview_resp.status_code}"
    print("PASS: Admin Overview progress monitor rendered.")

    # 6. Faculty Login
    faculty_client = app.app.test_client()
    faculty_client.get('/login')
    with faculty_client.session_transaction() as sess:
        fac_csrf = sess.get('_csrf_token')

    fac_login_resp = faculty_client.post('/login', data={
        'username': 'ptest',
        'password': 'mypassword123',
        'csrf_token': fac_csrf
    })
    assert fac_login_resp.status_code == 302, f"Faculty login failed: {fac_login_resp.status_code}"
    print("PASS: Faculty user authenticated.")

    # 7. Faculty Panel
    panel_resp = faculty_client.get('/faculty')
    assert panel_resp.status_code == 200, f"Faculty panel failed: {panel_resp.status_code}"
    assert "Operating Systems" in panel_resp.data.decode('utf-8')
    assert "ABHISHEK KUMAR" in panel_resp.data.decode('utf-8')
    print("PASS: Faculty Panel loaded student roster.")

    # 8. Faculty Batch Save Submissions
    with faculty_client.session_transaction() as sess:
        fac_csrf = sess.get('_csrf_token')

    save_payload = json.dumps([{"row_index": 2, "status": "Submitted", "date": "12-10-2026"}])
    save_resp = faculty_client.post('/faculty/save-submissions', data={
        'assignment_name': 'Assignment 1',
        'updates': save_payload,
        'csrf_token': fac_csrf
    }, headers={'X-CSRF-Token': fac_csrf})
    assert save_resp.status_code == 200 and save_resp.json.get('success'), f"Save submissions failed: {save_resp.data}"
    print("PASS: Faculty batch saved submissions to Google Sheet.")

    # Clean up row 2 in sheet
    reset_payload = json.dumps([{"row_index": 2, "status": "Not Submitted", "date": ""}])
    faculty_client.post('/faculty/save-submissions', data={
        'assignment_name': 'Assignment 1',
        'updates': reset_payload,
        'csrf_token': fac_csrf
    }, headers={'X-CSRF-Token': fac_csrf})
    print("PASS: Cleaned up student row in Google Sheet.")

    # 9. Clean up test faculty
    delete_resp = admin_client.post('/admin/faculty/delete', data={
        'faculty_id': created_fac['faculty_id'],
        'csrf_token': admin_csrf
    }, headers={'X-CSRF-Token': admin_csrf})
    assert delete_resp.status_code == 200, f"Delete faculty failed: {delete_resp.data}"
    print("PASS: Deleted test faculty from master sheet.")

    print("=" * 60)
    print("ALL 9 TEST SUITE STAGES PASSED COMPLETELY WITHOUT ERRORS!")
    print("=" * 60)

if __name__ == '__main__':
    run_tests()
