import os
import psycopg2
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, request, jsonify, session
from flask_cors import CORS
import google.generativeai as genai
import csv
import io

# --- Configuration ---
# Load the Gemini API key from environment variables for security
try:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        print("Warning: GEMINI_API_KEY environment variable not set.")
    genai.configure(api_key=GEMINI_API_KEY)
except Exception as e:
    print(f"Error configuring Gemini API: {e}")

# Use Heroku's DATABASE_URL if available, otherwise use a local one.
DB_URL = os.environ.get('DATABASE_URL', "postgresql://my_app_user:your_password@localhost/attendance_db")

app = Flask(__name__)
# A secret key is needed for session management
app.secret_key = os.environ.get('SECRET_KEY', 'a-super-secret-key-for-dev')
# Enable CORS to allow the frontend to communicate with the backend
CORS(app, supports_credentials=True)


# --- Database Connection Helper ---
def get_db_connection():
    conn = psycopg2.connect(DB_URL)
    return conn


# --- User Session/Auth Helpers ---
def login_required(role=None):
    def decorator(f):
        def wrapper(*args, **kwargs):
            if 'user_id' not in session:
                return jsonify({'message': 'Authentication required'}), 401
            if role and session.get('role') != role:
                return jsonify({'message': 'Forbidden'}), 403
            return f(*args, **kwargs)

        wrapper.__name__ = f.__name__
        return wrapper

    return decorator


# --- API Endpoints ---

# 1. Authentication
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    role = data.get('role')

    conn = get_db_connection()
    cursor = conn.cursor()

    user_data = None
    if role == 'admin':
        cursor.execute('SELECT id, username, password FROM admins WHERE username = %s', (username,))
        user_data = cursor.fetchone()
    elif role == 'teacher':
        cursor.execute('SELECT id, name, password FROM teachers WHERE email = %s AND is_approved = true', (username,))
        user_data = cursor.fetchone()
    elif role == 'student':
        cursor.execute('SELECT id, name, password FROM students WHERE username = %s', (username,))
        user_data = cursor.fetchone()

    cursor.close()
    conn.close()

    if user_data and check_password_hash(user_data[2], password):
        session['user_id'] = user_data[0]
        session['role'] = role
        session['name'] = user_data[1] if role != 'admin' else 'Admin'
        return jsonify({'success': True, 'user': {'id': user_data[0], 'name': session['name'], 'role': role}})

    return jsonify({'message': 'Invalid credentials or account not approved'}), 401


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/session', methods=['GET'])
def check_session():
    if 'user_id' in session:
        return jsonify(
            {'success': True, 'user': {'id': session['user_id'], 'name': session['name'], 'role': session['role']}})
    return jsonify({'success': False}), 401


@app.route('/api/signup/teacher', methods=['POST'])
def signup_teacher():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    class_id = data.get('class_id')
    phone = data.get('phone')

    if not all([name, email, password, class_id]):
        return jsonify({'message': 'Missing required fields'}), 400

    hashed_password = generate_password_hash(password)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'INSERT INTO teachers (name, email, password, class_id, phone) VALUES (%s, %s, %s, %s, %s)',
            (name, email, hashed_password, class_id, phone)
        )
        conn.commit()
    except psycopg2.IntegrityError:
        return jsonify({'message': 'Email address already exists'}), 409
    finally:
        cursor.close()
        conn.close()

    return jsonify({'message': 'Signup successful! Please wait for admin approval.'}), 201


# 2. General Data (for signup forms, etc.)
@app.route('/api/classes', methods=['GET'])
def get_classes():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name FROM classes ORDER BY name')
    classes = [{'id': row[0], 'name': row[1]} for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return jsonify({'classes': classes})


# 3. Admin Routes
@app.route('/api/admin/pending_teachers', methods=['GET'])
@login_required(role='admin')
def get_pending_teachers():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.id, t.name, t.email, t.phone, c.name as class_name 
        FROM teachers t 
        JOIN classes c ON t.class_id = c.id 
        WHERE t.is_approved = false
    ''')
    teachers = [{'id': r[0], 'name': r[1], 'email': r[2], 'phone': r[3], 'class_name': r[4]} for r in cursor.fetchall()]
    cursor.close()
    conn.close()
    return jsonify({'teachers': teachers})


@app.route('/api/admin/approve_teacher', methods=['POST'])
@login_required(role='admin')
def approve_teacher():
    data = request.get_json()
    teacher_id = data.get('teacher_id')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE teachers SET is_approved = true WHERE id = %s', (teacher_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'message': 'Teacher approved successfully'})


@app.route('/api/admin/classes', methods=['GET', 'POST'])
@login_required(role='admin')
def manage_classes():
    conn = get_db_connection()
    cursor = conn.cursor()
    if request.method == 'POST':
        name = request.json.get('name')
        cursor.execute('INSERT INTO classes (name) VALUES (%s)', (name,))
        conn.commit()
    cursor.execute('SELECT id, name FROM classes ORDER BY name')
    classes = [{'id': r[0], 'name': r[1]} for r in cursor.fetchall()]
    cursor.close()
    conn.close()
    return jsonify({'classes': classes})


@app.route('/api/admin/classes/<int:class_id>', methods=['DELETE'])
@login_required(role='admin')
def delete_class(class_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('DELETE FROM classes WHERE id = %s', (class_id,))
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        return jsonify({'message': f'Cannot delete class, it may be in use. DB Error: {e}'}), 400
    finally:
        cursor.close()
        conn.close()
    return jsonify({'message': 'Class deleted'})


# 4. Teacher Routes
@app.route('/api/teacher/my_class', methods=['GET'])
@login_required(role='teacher')
def get_teacher_class():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT c.id, c.name FROM classes c
        JOIN teachers t ON c.id = t.class_id
        WHERE t.id = %s
    ''', (session['user_id'],))
    class_info = cursor.fetchone()
    if not class_info:
        return jsonify({'message': 'Could not find assigned class'}), 404

    cursor.execute('''
        SELECT id, name, username FROM students
        WHERE class_id = %s ORDER BY name
    ''', (class_info[0],))
    students = [{'id': s[0], 'name': s[1], 'username': s[2]} for s in cursor.fetchall()]

    cursor.close()
    conn.close()
    return jsonify({
        'class': {'id': class_info[0], 'name': class_info[1]},
        'students': students
    })


# 5. NEW GEMINI API ROUTE
@app.route('/api/teacher/generate_report', methods=['POST'])
@login_required(role='teacher')
def generate_student_report():
    if not GEMINI_API_KEY:
        return jsonify({'message': 'Gemini API key is not configured on the server.'}), 500

    data = request.get_json()
    student_id = data.get('student_id')

    conn = get_db_connection()
    cursor = conn.cursor()

    # Get student name
    cursor.execute('SELECT name FROM students WHERE id = %s', (student_id,))
    student_name_row = cursor.fetchone()
    if not student_name_row:
        return jsonify({'message': 'Student not found'}), 404
    student_name = student_name_row[0]

    # Get last 30 days of attendance
    cursor.execute('''
        SELECT date, status FROM attendance
        WHERE student_id = %s
        ORDER BY date DESC
        LIMIT 30
    ''', (student_id,))

    attendance_records = cursor.fetchall()
    cursor.close()
    conn.close()

    if not attendance_records:
        return jsonify({'report': f"{student_name} has no recent attendance records."})

    # Format the data for the LLM prompt
    attendance_str = ", ".join([f"{record[0]}: {record[1]}" for record in attendance_records])

    prompt = f"""
    As a helpful teacher's assistant, analyze the following recent attendance data for a student named {student_name} and write a brief, constructive performance summary (2-4 sentences). 
    Focus on patterns, not just listing dates. Be positive if the attendance is good, or gently concerned if there are issues like frequent absences or half-days.

    Data: {attendance_str}
    """

    try:
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        response = model.generate_content(prompt)
        report_text = response.text
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return jsonify({'message': f'Failed to generate report due to an AI service error: {e}'}), 500

    return jsonify({'report': report_text})


# Run the app
if __name__ == '__main__':
    # Use 0.0.0.0 to be accessible on the network
    # The port is read from an environment variable for Heroku compatibility
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)

