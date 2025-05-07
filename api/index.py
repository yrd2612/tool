from flask import Flask, render_template, request, send_from_directory, jsonify, session
from PIL import Image, ImageDraw
import os
import json
from azure.storage.blob import BlobServiceClient
import uuid
from werkzeug.utils import secure_filename
import fcntl
import time

# Set your Azure Blob Storage credentials
CONNECTION_STRING = os.environ.get('CONNECTION_STRING')
CONTAINER_NAME = "test-screenshots"
BLOB_BASE_PATH = "test_screenshots/tagged_screenshot"

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Required for session management

# Configure upload folder
UPLOAD_FOLDER = 'user_data'
ANNOTATED_FOLDER = 'annotated_images'
DOWNLOAD_FOLDER = 'download_images'
ALLOWED_EXTENSIONS = {'json'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_user_folder(email):
    # Extract username from email (part before @)
    username = email.split('@')[0]
    user_folder = os.path.join(UPLOAD_FOLDER, username)
    os.makedirs(user_folder, exist_ok=True)
    os.makedirs(os.path.join(user_folder, ANNOTATED_FOLDER), exist_ok=True)
    os.makedirs(os.path.join(user_folder, DOWNLOAD_FOLDER), exist_ok=True)
    return user_folder

def safe_read_json(file_path, max_retries=3, retry_delay=0.1):
    """Safely read JSON file with file locking and retries"""
    for attempt in range(max_retries):
        try:
            with open(file_path, 'r') as f:
                # Get an exclusive lock
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    data = json.load(f)
                    return data
                finally:
                    # Release the lock
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (json.JSONDecodeError, IOError) as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(retry_delay)
    return None

def safe_write_json(file_path, data, max_retries=3, retry_delay=0.1):
    """Safely write JSON file with file locking and retries"""
    for attempt in range(max_retries):
        try:
            with open(file_path, 'w') as f:
                # Get an exclusive lock
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(data, f, indent=4)
                    return True
                finally:
                    # Release the lock
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except IOError as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(retry_delay)
    return False

def get_state_file_path(user_folder, original_json):
    """Get path for state file based on original JSON filename"""
    base_name = os.path.splitext(os.path.basename(original_json))[0]
    return os.path.join(user_folder, f"{base_name}_state.txt")

def get_updated_json_path(user_folder, original_json):
    """Get path for updated JSON file based on original JSON filename"""
    base_name = os.path.splitext(os.path.basename(original_json))[0]
    return os.path.join(user_folder, f"{base_name}_updated.json")

def ensure_updated_json_exists(updated_json_path, original_json_path, index):
    """Ensure the entry exists in updated_json, creating it if necessary"""
    try:
        # Read the original JSON to get the entry
        with open(original_json_path, 'r') as f:
            original_data = json.load(f)
        
        if index >= len(original_data):
            return None
        
        entry = original_data[index]
        
        # Initialize updated_json if it doesn't exist
        if not os.path.exists(updated_json_path):
            updated_data = []
        else:
            # Read existing updated_json
            with open(updated_json_path, 'r') as f:
                try:
                    content = f.read().strip()
                    # Remove any trailing invalid characters
                    if content.endswith(']}') or content.endswith('}]'):
                        content = content[:-2]
                    updated_data = json.loads(content)
                    if not isinstance(updated_data, list):
                        updated_data = []
                except json.JSONDecodeError:
                    updated_data = []
        
        # Ensure the entry exists in updated_data
        while len(updated_data) <= index:
            if len(updated_data) == index:
                # Create a copy of the entry with visited and counter fields
                entry_copy = entry.copy()
                entry_copy['visited'] = False
                entry_copy['counter'] = 0
                updated_data.append(entry_copy)
            else:
                # Add placeholder entries for skipped indices
                updated_data.append(None)
        
        # Save the updated JSON with proper formatting
        with open(updated_json_path, 'w') as f:
            json.dump(updated_data, f, indent=4)
        
        return updated_data[index]
    except Exception as e:
        print(f"Error ensuring updated JSON exists: {str(e)}")
        return None

def save_state(user_folder, original_json, current_index):
    """Save current state to state file"""
    state_file = get_state_file_path(user_folder, original_json)
    with open(state_file, 'w') as f:
        f.write(str(current_index))

def load_state(user_folder, original_json):
    """Load current state from state file"""
    state_file = get_state_file_path(user_folder, original_json)
    if os.path.exists(state_file):
        with open(state_file, 'r') as f:
            try:
                return int(f.read().strip())
            except ValueError:
                return 0
    return 0

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/upload_json', methods=['POST'])
def upload_json():
    if 'email' not in request.form:
        return jsonify({'success': False, 'message': 'Email is required'}), 400
    
    email = request.form['email']
    user_folder = get_user_folder(email)
    
    if 'json_file' not in request.files:
        return jsonify({'success': False, 'message': 'No file uploaded'}), 400
    
    file = request.files['json_file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        original_json_path = os.path.join(user_folder, filename)
        updated_json_path = get_updated_json_path(user_folder, original_json_path)
        
        try:
            # Save the uploaded file
            file.save(original_json_path)
            
            # Read the original JSON to get total entries
            with open(original_json_path, 'r') as f:
                data = json.load(f)
            
            # Initialize state
            save_state(user_folder, original_json_path, 0)
            
            # Store user session info
            session['user_email'] = email
            session['original_json'] = original_json_path
            session['updated_json'] = updated_json_path
            session['current_index'] = 0
            
            # Get or create the first entry in updated_json
            first_entry = ensure_updated_json_exists(updated_json_path, original_json_path, 0)
            
            return jsonify({
                'success': True,
                'message': 'File uploaded successfully',
                'total_entries': len(data),
                'current_index': 0,
                'entry': first_entry
            })
        except Exception as e:
            return jsonify({'success': False, 'message': f'Error processing JSON: {str(e)}'}), 500
    
    return jsonify({'success': False, 'message': 'Invalid file type'}), 400

@app.route('/get_entry/<int:index>', methods=['GET'])
def get_entry(index):
    if 'user_email' not in session or 'updated_json' not in session:
        return jsonify({'success': False, 'message': 'No active session'}), 400
    
    try:
        # Read the original JSON to get total entries
        with open(session['original_json'], 'r') as f:
            original_data = json.load(f)
            total_entries = len(original_data)
        
        if index < 0 or index >= total_entries:
            return jsonify({'success': False, 'message': 'Index out of bounds'}), 400
        
        # Ensure the entry exists in updated_json
        entry = ensure_updated_json_exists(
            session['updated_json'],
            session['original_json'],
            index
        )
        
        if not entry:
            return jsonify({'success': False, 'message': 'Error processing entry'}), 500
        
        # Update visited status and counter
        entry['visited'] = True
        entry['counter'] = index + 1
        
        # Read the current updated JSON
        with open(session['updated_json'], 'r') as f:
            try:
                content = f.read().strip()
                # Remove any trailing invalid characters
                if content.endswith(']}') or content.endswith('}]'):
                    content = content[:-2]
                data = json.loads(content)
                if not isinstance(data, list):
                    data = []
            except json.JSONDecodeError:
                data = []
        
        # Ensure the data array is long enough
        while len(data) <= index:
            data.append(None)
        
        # Update the entry
        data[index] = entry
        
        # Save the updated JSON with proper formatting
        with open(session['updated_json'], 'w') as f:
            json.dump(data, f, indent=4)
        
        # Update state
        save_state(get_user_folder(session['user_email']), session['original_json'], index)
        
        # Update session
        session['current_index'] = index
        
        # Ensure the image exists locally
        image_path = entry.get('image_path') or entry.get('tagified_image_url', '')
        if not image_path:
            return jsonify({'success': False, 'message': 'No image path found in entry.'}), 400
            
        filename = os.path.basename(image_path)
        user_folder = get_user_folder(session['user_email'])
        download_folder = os.path.join(user_folder, DOWNLOAD_FOLDER)
        
        if not os.path.exists(os.path.join(download_folder, filename)):
            try:
                # Try to download from Azure if not available locally
                download_from_azure(filename, user_folder)
            except Exception as e:
                return jsonify({'success': False, 'message': f'Error downloading image: {str(e)}'}), 500
        
        return jsonify({
            'success': True,
            'total_entries': total_entries,
            'current_index': index,
            'entry': entry
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error processing entry: {str(e)}'}), 500

def download_from_azure(filename, user_folder):
    """Helper function to download image from Azure Blob Storage"""
    print(f"Downloading from Azure: {filename}")
    blob_service_client = BlobServiceClient.from_connection_string(CONNECTION_STRING)
    print(f"file name: {filename}")
    # Construct the full blob path, assuming Azure files end with .png
    full_blob_path = f"{BLOB_BASE_PATH}/{filename.split('.')[0]}.png"  # Split to remove any extension and then add .png
    print(f"Downloading from Azure: {full_blob_path}")
    
    # Create a BlobClient
    blob_client = blob_service_client.get_blob_client(
        container=CONTAINER_NAME,
        blob=full_blob_path
    )

    # Define the local path to save the image
    download_folder = os.path.join(user_folder, DOWNLOAD_FOLDER)
    image_download_path = os.path.join(download_folder, filename)
    
    try:
        # Download the blob
        with open(image_download_path, "wb") as image_file:
            image_file.write(blob_client.download_blob().readall())
        
        # Try to open and convert the image to ensure it's in a format that supports enough colors
        try:
            img = Image.open(image_download_path).convert('RGB')
            img.save(image_download_path, format='JPEG')
        except Exception as e:
            print(f"Warning: Could not convert image format: {str(e)}")
        
        return image_download_path
    except Exception as e:
        print(f"Error downloading from Azure: {str(e)}")
        raise

@app.route('/save_annotation', methods=['POST'])
def save_annotation():
    if 'user_email' not in session or 'updated_json' not in session:
        return jsonify({'success': False, 'message': 'No active session'}), 400
    
    try:
        data = request.get_json()
        operation_id = data.get('operation_id')
        
        # Read the current updated JSON
        with open(session['updated_json'], 'r') as f:
            try:
                content = f.read().strip()
                # Remove any trailing invalid characters
                if content.endswith(']}') or content.endswith('}]'):
                    content = content[:-2]
                json_data = json.loads(content)
                if not isinstance(json_data, list):
                    json_data = []
            except json.JSONDecodeError:
                json_data = []
        
        # Find and update the entry
        for i, entry in enumerate(json_data):
            if entry and entry.get('operation_id') == operation_id:
                # Update the entry while preserving existing fields
                entry.update({
                    'operation_intent': data.get('operation_intent', entry.get('operation_intent')),
                    'element_rect': data.get('element_rect', entry.get('element_rect')),
                    'image_path': data.get('image_path', entry.get('image_path')),
                    'entry_number': data.get('entry_number', entry.get('entry_number')),
                    'tag': data.get('tag', entry.get('tag')),
                    'visited': True,
                    'counter': data.get('entry_number', entry.get('counter'))
                })
                break
        
        # Save the updated JSON with proper formatting
        with open(session['updated_json'], 'w') as f:
            json.dump(json_data, f, indent=4)
        
        # Save annotated image
        try:
            user_folder = get_user_folder(session['user_email'])
            filename = os.path.basename(data.get('image_path'))
            original_path = os.path.join(user_folder, DOWNLOAD_FOLDER, filename)
            
            if os.path.exists(original_path):
                annotated_folder = os.path.join(user_folder, ANNOTATED_FOLDER)
                annotated_image_path = os.path.join(annotated_folder, f"annotated_{filename}")
                
                img = Image.open(original_path).convert('RGB')
                draw = ImageDraw.Draw(img)
                
                element_rect = data.get('element_rect', {})
                draw.rectangle([(element_rect['x'], element_rect['y']), 
                              (element_rect['x'] + element_rect['width'], 
                               element_rect['y'] + element_rect['height'])], 
                             outline="red", width=2)
                
                annotated_image_path = annotated_image_path.replace('.png', '.jpg')
                img.save(annotated_image_path, 'JPEG', quality=95)
                print(f"Saved annotated image to {annotated_image_path}")
        except Exception as img_e:
            print(f"Error saving annotated image: {str(img_e)}")
        
        return jsonify({
            'success': True,
            'message': 'Annotation saved successfully',
            'file': session['updated_json']
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error saving annotation: {str(e)}'}), 500

@app.route('/download_json', methods=['GET'])
def download_json():
    if 'user_email' not in session or 'updated_json' not in session:
        return jsonify({'success': False, 'message': 'No active session'}), 400
    
    try:
        return send_from_directory(
            os.path.dirname(session['updated_json']),
            os.path.basename(session['updated_json']),
            as_attachment=True
        )
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error downloading file: {str(e)}'}), 500

@app.route('/download_images/<path:filename>')
def get_download_image(filename):
    """Serve an image file from the user's download folder"""
    if 'user_email' not in session:
        return "No active session", 401
    
    user_folder = get_user_folder(session['user_email'])
    download_folder = os.path.join(user_folder, DOWNLOAD_FOLDER)
    
    if not os.path.exists(os.path.join(download_folder, filename)):
        try:
            download_from_azure(filename, user_folder)
        except Exception as e:
            return f"Error downloading image: {str(e)}", 500
    
    return send_from_directory(download_folder, filename)

@app.route('/annotated_images/<path:filename>')
def get_annotated_image(filename):
    """Serve an annotated image file from the user's annotated folder"""
    if 'user_email' not in session:
        return "No active session", 401
    
    user_folder = get_user_folder(session['user_email'])
    annotated_folder = os.path.join(user_folder, ANNOTATED_FOLDER)
    return send_from_directory(annotated_folder, filename)

if __name__ == '__main__':
    app.run(debug=True)