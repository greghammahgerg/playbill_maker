import json
import os
import re
import secrets
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps

import bleach
from flask import (
    Flask, abort, jsonify, redirect, render_template, request, send_from_directory,
    session, url_for,
)
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename
from dotenv import load_dotenv


import io

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import google.auth




load_dotenv()

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true',
)

RATE_LIMIT_BURSTS = defaultdict(deque)
RATE_LIMIT_WINDOW_SECONDS = 300
RATE_LIMIT_MAX_REQUESTS = 5

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPLOAD_FOLDER = os.path.join('static', 'uploads')
DATA_FILE = os.path.join('instance', 'submissions.json')
SEASON_FILE = os.path.join('instance', 'seasonal_program.json')

# Google Drive Credentials File Path
SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_CREDENTIALS_PATH', 'credentials.json')

def _parse_leadership_names(raw):
    """Parse 'First|Last,First|Last' from the LEADERSHIP_NAMES env var
    into a tuple of (first, last) tuples."""
    pairs = []
    for entry in (raw or '').split(','):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split('|')
        if len(parts) != 2:
            print(f'WARNING: skipping malformed LEADERSHIP_NAMES entry: "{entry}"')
            continue
        first, last = (p.strip() for p in parts)
        pairs.append((first, last))
    return tuple(pairs)


LEADERSHIP_NAMES = _parse_leadership_names(os.getenv('LEADERSHIP_NAMES'))
ADMIN_EMAILS = {
    email.strip().casefold()
    for email in os.getenv('ADMIN_EMAILS', '').split(',')
    if email.strip()
}

FIREBASE_WEB_CONFIG = {
    'apiKey': os.getenv('FIREBASE_API_KEY'),
    'authDomain': os.getenv('FIREBASE_AUTH_DOMAIN'),
    'projectId': os.getenv('FIREBASE_PROJECT_ID'),
    'appId': os.getenv('FIREBASE_APP_ID'),
}
FIREBASE_WEB_CONFIGURED = all(FIREBASE_WEB_CONFIG.values())

if not LEADERSHIP_NAMES:
    print('WARNING: LEADERSHIP_NAMES is not set (or empty) in .env — '
          'no submissions will be treated as leadership.')



ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

MIN_IMAGE_BYTES = 1 * 1024 * 1024        # 1 MB
MAX_IMAGE_BYTES = 10 * 1024 * 1024       # 10 MB

MIN_BIO_WORDS = 50
MAX_BIO_WORDS = 150

# Uploaded photos retain their original proportions. This bounds the saved
# web image without cropping it, while retaining enough detail for display.
HEADSHOT_MAX_BOUNDS = (1000, 1000)

# Drop the logo file here (relative to this app.py). Matches the path you
# already have it saved at: assets\SHCM Circle Logo.png
LOGO_PATH = os.path.join('assets', 'SHCM Circle Logo.png')

# Rich text is allowed to keep italics/underline (and basic paragraph
# breaks from pasted content), but nothing else. Anything not in this list
# is stripped OUT of the markup while the underlying text is kept, so bold
# text just becomes plain text instead of being rejected outright.
ALLOWED_BIO_TAGS = ['i', 'em', 'u', 'p', 'br']
ALLOWED_BIO_ATTRS = {}  # no attributes allowed -> kills style="font-size:..." etc.

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # hard cap; real check is per-file below
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', secrets.token_urlsafe(32))

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs('instance', exist_ok=True)
os.makedirs('assets', exist_ok=True)

if not os.path.exists(LOGO_PATH):
    print(
        f'WARNING: logo not found at "{LOGO_PATH}". '
        f'The site will still work, just without the logo. '
        f'Save your file there (or update LOGO_PATH) to include it.'
    )


# ---------------------------------------------------------------------------
# Tiny JSON "database" keyed by a slug of the musician's full name.
# This is what makes a second submission from the same person overwrite
# the first instead of piling up new files.
# ---------------------------------------------------------------------------
def load_submissions():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_submissions(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def load_season_program():
    if not os.path.exists(SEASON_FILE):
        return {'selected_submission_ids': []}
    with open(SEASON_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_season_program(data):
    with open(SEASON_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def slugify(name):
    """Turn a full name into a stable, filesystem-safe identifier."""
    slug = name.strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '_', slug).strip('_')
    return slug or 'unknown'


def normalized_name(name):
    name = re.sub(r'^(mr|mrs|ms|dr)\.?\s+', '', name.strip(), flags=re.IGNORECASE)
    return re.sub(r'[^a-z0-9]+', ' ', name.lower()).strip()


def leadership_rank(submission):
    first = normalized_name(submission.get('first_name', ''))
    last = normalized_name(submission.get('last_name', ''))
    for index, (leader_first, leader_last) in enumerate(LEADERSHIP_NAMES):
        if first == normalized_name(leader_first) and last == normalized_name(leader_last):
            return index
    return None


def inferred_last_name(submission):
    if submission.get('last_name'):
        return submission['last_name']

    name_parts = normalized_name(submission.get('name', '')).split()
    while name_parts and name_parts[-1] in {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}:
        name_parts.pop()
    return name_parts[-1] if name_parts else ''


def ordered_season_submissions(submissions, selected_ids):
    selected = [
        (submission_id, submissions[submission_id])
        for submission_id in selected_ids
        if submission_id in submissions
    ]
    leadership = []
    artists = []

    for submission_id, submission in selected:
        rank = leadership_rank(submission)
        if rank is None:
            artists.append((submission_id, submission))
        else:
            leadership.append((rank, submission_id, submission))

    leadership.sort(key=lambda item: item[0])
    artists.sort(key=lambda item: (
        inferred_last_name(item[1]).casefold(),
        item[1].get('name', '').casefold(),
    ))
    return [(submission_id, submission) for _, submission_id, submission in leadership] + artists


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return view(*args, **kwargs)
    return wrapped_view


def artist_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get('user_email') or not session.get('email_verified'):
            return redirect(url_for('artist_login', next=request.path))
        return view(*args, **kwargs)
    return wrapped_view


def safe_next_url(value, default):
    if not value or not value.startswith('/') or value.startswith('//'):
        return default
    return value


def verify_firebase_id_token(id_token):
    if not id_token:
        raise ValueError('Missing sign-in token.')

    try:
        import firebase_admin
        from firebase_admin import auth, credentials

        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app()

        claims = auth.verify_id_token(id_token, check_revoked=True)
    except ValueError:
        raise
    except Exception as exc:
        app.logger.warning('Firebase sign-in verification failed: %s', exc)
        raise ValueError('Your sign-in could not be verified. Please try again.') from exc

    email = claims.get('email', '').strip().casefold()
    if not email or not claims.get('email_verified'):
        raise ValueError('Please sign in with a verified email address.')
    return claims, email


def get_client_ip():
    forwarded_for = request.headers.get('X-Forwarded-For', '')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip() or request.remote_addr or 'unknown'
    return request.remote_addr or 'unknown'


def is_rate_limited(endpoint, limit=RATE_LIMIT_MAX_REQUESTS, window_seconds=RATE_LIMIT_WINDOW_SECONDS):
    bucket_key = f'{endpoint}:{get_client_ip()}'
    bucket = RATE_LIMIT_BURSTS[bucket_key]
    now = datetime.now(timezone.utc).timestamp()

    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()

    if len(bucket) >= limit:
        return True

    bucket.append(now)
    return False


def require_admin_csrf():
    submitted_token = request.form.get('csrf_token', '')
    expected_token = session.get('csrf_token', '')
    if not expected_token or not secrets.compare_digest(submitted_token, expected_token):
        abort(400)


def get_safe_upload_path(filename, slug):
    base_name = secure_filename(slug or 'submission')
    original_extension = os.path.splitext(secure_filename(filename or ''))[1].lower()
    if not original_extension:
        raise ValueError('Missing file extension for upload.')

    extension = original_extension
    if extension.lstrip('.') not in ALLOWED_EXTENSIONS:
        raise ValueError('Invalid file type. Please upload a JPG, PNG, or WebP image.')

    destination_name = f'{base_name}{extension}'
    upload_dir = os.path.abspath(app.config['UPLOAD_FOLDER'])
    destination = os.path.abspath(os.path.join(upload_dir, destination_name))
    if os.path.commonpath([upload_dir, destination]) != upload_dir:
        raise ValueError('Invalid upload path.')
    return destination


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def allowed_file(filename):
    """Check if the uploaded file has a valid image extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_size(file_storage):
    """Determine upload size without reading it all into memory at once."""
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    return size


def sanitize_bio_html(raw_html):
    """Keep italics/underline/paragraph structure, strip everything else
    (bold, font tags, inline styles/sizes) while preserving the text."""
    # Normalize non-breaking spaces (common from rich-text editors) to
    # regular spaces before cleaning, so they don't leak into output.
    raw_html = (raw_html or '').replace('&nbsp;', ' ').replace('\xa0', ' ')
    cleaned = bleach.clean(
        raw_html,
        tags=ALLOWED_BIO_TAGS,
        attributes=ALLOWED_BIO_ATTRS,
        strip=True,
    )
    return cleaned.strip()


def count_words(html):
    """Word count based on visible text only, ignoring any markup."""
    text = bleach.clean(html or '', tags=[], attributes={}, strip=True)
    words = [w for w in re.split(r'\s+', text.strip()) if w]
    return len(words)






# IMAGE PROCESSING - TO DRIVE AND TO MOBILE CONCERT PROGRAM SHEET

# Drive API scope required for full folder and file creation
SCOPES = ['https://www.googleapis.com/auth/drive']

# Pull the Master Folder ID from environment variables (.env locally or Cloud Run env vars)
PARENT_DRIVE_FOLDER_ID = os.getenv('PARENT_DRIVE_FOLDER_ID')

def get_drive_service():
    """Authenticates using Application Default Credentials and returns the Drive API client."""
    creds, _ = google.auth.default(scopes=SCOPES)
    return build('drive', 'v3', credentials=creds)

def create_gdrive_folder(service, folder_name, parent_id):
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = service.files().create(
        body=file_metadata, fields='id', supportsAllDrives=True
    ).execute()
    return folder.get('id')

def handle_artist_submission(file_storage, last_name, first_name, full_name, bio_html, local_web_dest):
    """
    1. Creates a 'Last First' folder on Google Drive (best-effort).
    2. Uploads the raw image (best-effort).
    3. Uploads a text file containing the artist's name and bio (best-effort).
    4. Processes a lightweight thumbnail for the local web preview (required).

    Steps 1-3 are wrapped so that a Drive failure (missing credentials,
    network issue, bad folder ID, etc.) never blocks a submission from
    being saved locally -- Drive is a nice-to-have backup, not the
    source of truth for the site itself.
    """
    drive_uploaded = False
    try:
        folder_name = f"{last_name.strip()} {first_name.strip()}"
        _, file_extension = os.path.splitext(file_storage.filename)
        drive_image_filename = f"{last_name.strip()}_{first_name.strip()}_raw{file_extension}"
        drive_text_filename = f"{last_name.strip()}_{first_name.strip()}_bio.txt"

        service = get_drive_service()

        # -----------------------------------------------------------------
        # STEP 1: Create the custom Artist Folder
        # -----------------------------------------------------------------
        print(f"Creating Google Drive folder: '{folder_name}'...")
        artist_folder_id = create_gdrive_folder(service, folder_name, PARENT_DRIVE_FOLDER_ID)

        # -----------------------------------------------------------------
        # STEP 2: Upload Raw File to Drive
        # -----------------------------------------------------------------
        print(f"Uploading pristine raw file '{drive_image_filename}'...")
        file_storage.stream.seek(0)
        media_img = MediaIoBaseUpload(file_storage.stream, mimetype=file_storage.mimetype, resumable=True)
        img_metadata = {'name': drive_image_filename, 'parents': [artist_folder_id]}

        service.files().create(body=img_metadata, media_body=media_img, fields='id', supportsAllDrives=True).execute()

        # -----------------------------------------------------------------
        # STEP 3: Upload Text File (Name & Bio) to Drive
        # -----------------------------------------------------------------
        print(f"Uploading text data '{drive_text_filename}'...")
        # Format the text document content
        text_content = f"Name: {full_name}\n\nBiography:\n{bio_html}"

        # Convert the string to a byte stream so Drive can read it like a file
        text_bytes = io.BytesIO(text_content.encode('utf-8'))
        media_text = MediaIoBaseUpload(text_bytes, mimetype='text/plain', resumable=True)
        text_metadata = {'name': drive_text_filename, 'parents': [artist_folder_id]}

        service.files().create(body=text_metadata, media_body=media_text, fields='id', supportsAllDrives=True).execute()

        drive_uploaded = True
        print("Drive upload completed successfully.")
    except Exception as exc:
        # Drive is best-effort -- log it loudly so it doesn't go unnoticed,
        # but let the submission continue so the artist isn't blocked.
        print(f'WARNING: Google Drive upload failed, continuing without it. Reason: {exc}')

    # -----------------------------------------------------------------
    # STEP 4: Process Local Web-Optimized Thumbnail
    # -----------------------------------------------------------------
    print("Processing local web-optimized thumbnail...")
    file_storage.stream.seek(0)

    os.makedirs(os.path.dirname(local_web_dest), exist_ok=True)
    with Image.open(file_storage.stream) as image:
        image = ImageOps.exif_transpose(image)
        image = image.convert('RGB')
        image.thumbnail(HEADSHOT_MAX_BOUNDS, resample=Image.Resampling.LANCZOS)
        image.save(local_web_dest, format='JPEG', quality=85)
    print("Submission pipeline completed successfully!")

    return drive_uploaded


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route('/form', methods=['GET', 'POST'])
@artist_required
def musician_form():
    if request.method == 'POST':
        if request.form.get('website'):
            return render_template(
                'form.html',
                errors=['Your submission was blocked as a bot attempt.'],
            ), 400

        if is_rate_limited('musician_form'):
            return render_template(
                'form.html',
                errors=['Too many submissions from this address. Please try again later.'],
            ), 429

        errors = []

        prefix = (request.form.get('prefix') or '').strip()
        first_name = (request.form.get('first-name') or '').strip()
        middle_name = (request.form.get('middle-name') or '').strip()
        last_name = (request.form.get('last-name') or '').strip()
        suffix = (request.form.get('suffix') or '').strip()

        if not first_name or not last_name:
            errors.append('First and last name are required.')

        name_parts = [prefix, first_name, middle_name, last_name, suffix]
        name = ' '.join(p for p in name_parts if p)

        bio_raw_html = request.form.get('bio', '')
        bio_clean_html = sanitize_bio_html(bio_raw_html)
        word_count = count_words(bio_clean_html)
        if not (MIN_BIO_WORDS <= word_count <= MAX_BIO_WORDS):
            errors.append(
                f'Bio must be between {MIN_BIO_WORDS} and {MAX_BIO_WORDS} words '
                f'(currently {word_count}).'
            )

        file = request.files.get('headshot')
        if not file or file.filename == '':
            errors.append('A headshot image is required.')
        elif not allowed_file(file.filename):
            errors.append('Invalid file type. Please upload a JPG, PNG, or WebP image.')
        else:
            size = get_file_size(file)
            if size < MIN_IMAGE_BYTES or size > MAX_IMAGE_BYTES:
                errors.append(
                    f'Image must be between 1MB and 10MB (yours is '
                    f'{size / (1024 * 1024):.1f}MB).'
                )

        if errors:
            return render_template('form.html', errors=errors)

        slug = slugify(name)
        try:
            headshot_path = get_safe_upload_path(f'{slug}.jpg', slug)
        except ValueError as exc:
            return render_template('form.html', errors=[str(exc)])

        headshot_filename = os.path.basename(headshot_path)

        # Call the new Drive pipeline, passing the names and bio data
        handle_artist_submission(
            file_storage=file, 
            last_name=last_name, 
            first_name=first_name, 
            full_name=name, 
            bio_html=bio_clean_html, 
            local_web_dest=headshot_path
        )

        submissions = load_submissions()
        submissions[slug] = {
            'name': name,
            'first_name': first_name,
            'last_name': last_name,
            'bio_html': bio_clean_html,
            'headshot_filename': headshot_filename,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        save_submissions(submissions)

        return (
            "<div style='text-align:center;'><h3>Success! Your info and "
            "headshot have been saved.</h3></div>"
        )

    return render_template('form.html', errors=None, user_email=session['user_email'])


@app.route('/')
def mobile_artist_profiles():
    return render_template('mobile_template.html')


@app.get('/form/login')
def artist_login():
    return render_template(
        'firebase_login.html',
        firebase_config=FIREBASE_WEB_CONFIG if FIREBASE_WEB_CONFIGURED else None,
        next_url=safe_next_url(request.args.get('next'), url_for('musician_form')),
        audience='Artist Submission',
        message='Sign in with Google to submit or update your artist information.',
    )


@app.get('/admin/login')
def admin_login():
    return render_template(
        'firebase_login.html',
        firebase_config=FIREBASE_WEB_CONFIG if FIREBASE_WEB_CONFIGURED else None,
        next_url=url_for('admin_dashboard'),
        audience='Program Admin',
        message='Sign in with an approved Google account to manage the seasonal lineup.',
    )


@app.post('/auth/session')
def create_auth_session():
    data = request.get_json(silent=True) or {}
    next_url = safe_next_url(data.get('next'), url_for('musician_form'))

    try:
        claims, email = verify_firebase_id_token(data.get('idToken'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 401

    is_admin_login = next_url == url_for('admin_dashboard')
    if is_admin_login and email not in ADMIN_EMAILS:
        return jsonify({'error': 'This Google account is not approved for admin access.'}), 403

    session.clear()
    session['user_uid'] = claims['uid']
    session['user_email'] = email
    session['email_verified'] = True
    session['is_admin'] = is_admin_login
    session['csrf_token'] = secrets.token_urlsafe(32)
    return jsonify({'redirect': next_url})


@app.post('/auth/logout')
def auth_logout():
    session.clear()
    return ('', 204)


@app.route('/admin', methods=['GET', 'POST'])
@admin_required
def admin_dashboard():
    submissions = load_submissions()
    season_program = load_season_program()

    if request.method == 'POST':
        require_admin_csrf()

        selected_ids = [
            submission_id
            for submission_id in request.form.getlist('submission_ids')
            if submission_id in submissions
        ]
        try:
            save_season_program({'selected_submission_ids': selected_ids})
        except OSError:
            app.logger.exception('Unable to save seasonal lineup')
            return redirect(url_for('admin_dashboard', saved=0))
        return redirect(url_for('admin_dashboard', saved=1))

    selected_ids = set(season_program.get('selected_submission_ids', []))
    candidates = []
    for submission_id, submission in submissions.items():
        rank = leadership_rank(submission)
        candidates.append({
            'id': submission_id,
            'name': submission.get('name', ''),
            'last_name': inferred_last_name(submission),
            'updated_at': submission.get('updated_at', ''),
            'is_leadership': rank is not None,
            'leadership_order': rank if rank is not None else len(LEADERSHIP_NAMES),
        })

    candidates.sort(key=lambda candidate: (
        candidate['leadership_order'],
        candidate['last_name'].casefold(),
        candidate['name'].casefold(),
    ))
    return render_template(
        'admin_dashboard.html',
        candidates=candidates,
        selected_ids=selected_ids,
        csrf_token=session['csrf_token'],
    )


@app.post('/admin/delete/<submission_id>')
@admin_required
def admin_delete_submission(submission_id):
    require_admin_csrf()

    submissions = load_submissions()
    submission = submissions.get(submission_id)
    if submission is None:
        abort(404)

    # Remove the local headshot only. Drive files are left untouched.
    headshot_filename = submission.get('headshot_filename')
    if headshot_filename:
        headshot_path = os.path.join(app.config['UPLOAD_FOLDER'], headshot_filename)
        try:
            os.remove(headshot_path)
        except FileNotFoundError:
            pass

    del submissions[submission_id]
    save_submissions(submissions)

    # Drop it from the season program too, so a stale id doesn't linger.
    season_program = load_season_program()
    selected_ids = season_program.get('selected_submission_ids', [])
    if submission_id in selected_ids:
        season_program['selected_submission_ids'] = [
            sid for sid in selected_ids if sid != submission_id
        ]
        save_season_program(season_program)

    return redirect(url_for('admin_dashboard'))



@app.post('/admin/logout')
@admin_required
def admin_logout():
    require_admin_csrf()
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/assets/<path:filename>')
def asset_file(filename):
    return send_from_directory('assets', filename)


@app.route('/api/artists')
def artist_data():
    submissions = load_submissions()
    season_program = load_season_program()
    artists = []

    for slug, submission in ordered_season_submissions(
        submissions, season_program.get('selected_submission_ids', [])
    ):
        headshot_filename = submission.get('headshot_filename')
        artists.append({
            'id': slug,
            'name': submission.get('name', ''),
            'last_name': inferred_last_name(submission),
            'bio_html': submission.get('bio_html', ''),
            'image_url': (
                url_for('static', filename=f'uploads/{headshot_filename}')
                if headshot_filename else None
            ),
        })

    return jsonify(artists)




if __name__ == '__main__':
    # Only turns on debug mode if FLASK_DEBUG=true is in your .env file
    is_debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=is_debug)