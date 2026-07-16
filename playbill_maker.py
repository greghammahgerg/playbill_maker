import os
from flask import Flask, render_template, request
from werkzeug.utils import secure_filename
import pdfkit

app = Flask(__name__)

# Configuration
UPLOAD_FOLDER = os.path.join('static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Point PDFKit directly to the default Windows installation folder path
path_to_wkhtmltopdf = r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"
pdf_config = pdfkit.configuration(wkhtmltopdf=path_to_wkhtmltopdf)


def allowed_file(filename):
    """Check if the uploaded file has a valid image extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def musician_form():
    if request.method == 'POST':
        name = request.form.get('name')
        bio = request.form.get('bio')
        file = request.files.get('headshot')

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            unique_filename = f"{secure_filename(name)}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
            file.save(filepath)
            
            abs_image_path = os.path.abspath(filepath)
            generate_playbill_pdf(name, bio, abs_image_path)
            
            return "<h3>Success! Your data and headshot have been saved, and your playbill PDF has been generated.</h3>"
        
        return "Invalid file type. Please upload a JPG, PNG, or WebP image."

    return render_template('form.html')

def generate_playbill_pdf(name, bio, image_path):
    """Compiles the HTML template with the form data and renders a PDF using PDFKit."""
    rendered_html = render_template('playbill_template.html', name=name, bio=bio, image_path=image_path)
    output_pdf_path = f"playbill_{secure_filename(name)}.pdf"
    
    # Configure print page settings for Half-Letter dimensions
    options = {
        'page-width': '5.5in',
        'page-height': '8.5in',
        'margin-top': '0.5in',
        'margin-right': '0.5in',
        'margin-bottom': '0.5in',
        'margin-left': '0.5in',
        'encoding': "UTF-8",
        'enable-local-file-access': None # Crucial flag to let the engine read your headshot image file
    }
    
    # Generate the file using the local engine configuration
    pdfkit.from_string(rendered_html, output_pdf_path, configuration=pdf_config, options=options)
    print(f"Generated: {output_pdf_path}")

if __name__ == '__main__':
    app.run(debug=True)
