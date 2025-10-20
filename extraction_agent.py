import oci
import json
import io
import logging
import os
import re
import importlib
import tempfile
from datetime import datetime
from fdk import response as fdk_response
import google.generativeai as genai
import PyPDF2

# ==================== LOGGING SETUP ====================
logging.getLogger().setLevel(logging.INFO)

# ==================== CONFIGURATION LOADING ====================
# Load configuration from environment variables or config file
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OCI_BUCKET_NAME = os.getenv("OCI_BUCKET_NAME")

if not GEMINI_API_KEY:
    logging.error("Missing GEMINI_API_KEY environment variable.")
    try:
        ENV = os.getenv("ENV", "AGENT").upper()
        l_env = importlib.import_module(f"config_{ENV}")
        GEMINI_API_KEY = l_env.GEMINI_API_KEY
        OCI_BUCKET_NAME = l_env.OCI_BUCKET_NAME
        logging.info("Configuration loaded from config_AGENT.py for local testing.")
    except ModuleNotFoundError:
        logging.error("No environment variables or config_AGENT.py found for local testing.")
        if not GEMINI_API_KEY:
            raise Exception("Essential Gemini API configuration is missing. Cannot proceed.")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)

# ==================== OCI CLIENTS ====================
object_storage_client = None
namespace = None

def init_oci_clients():
    global object_storage_client, namespace
    if object_storage_client is None:
        try:
            signer = oci.auth.signers.get_resource_principals_signer()
            logging.info("Authenticated using Resource Principal for OCI Object Storage.")
            config_for_rp = {"region": signer.region}
            object_storage_client = oci.object_storage.ObjectStorageClient(config_for_rp, signer=signer)
        except Exception:
            logging.warning("Resource Principal not available. Falling back to default OCI config (local testing).")
            oci_config = oci.config.from_file(
                oci.config.DEFAULT_LOCATION,
                oci.config.DEFAULT_PROFILE
            )
            object_storage_client = oci.object_storage.ObjectStorageClient(oci_config)
        namespace = object_storage_client.get_namespace().data
    return object_storage_client, namespace

# ==================== FILE PROCESSING ====================

def download_and_extract_text_from_oci(object_storage_client, namespace, bucket_name, object_name):
    """
    Downloads a file from OCI Object Storage and extracts text from it.
    Supports PDF and image files.
    """
    try:
        logging.info(f"Downloading file {object_name} from bucket {bucket_name}")
        
        # Download the file from OCI Object Storage
        response = object_storage_client.get_object(namespace, bucket_name, object_name)
        file_data = response.data.content
        
        logging.info(f"Downloaded file {object_name}, size: {len(file_data)} bytes")
        
        # Determine file type and extract text accordingly
        file_extension = object_name.lower().split('.')[-1]
        
        if file_extension == 'pdf':
            return extract_text_from_pdf(file_data)
        elif file_extension in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff']:
            return extract_text_from_image(file_data)
        else:
            # Try to decode as text
            try:
                return file_data.decode('utf-8')
            except UnicodeDecodeError:
                return f"Unsupported file type: {file_extension}"
                
    except Exception as e:
        logging.error(f"Failed to download and extract text from {object_name}: {e}", exc_info=True)
        return f"Error processing file: {e}"

def extract_text_from_pdf(pdf_data):
    """Extract text from PDF data using a simple approach."""
    try:
        logging.info("Extracting text from PDF using PyPDF2")
        
        # Create a BytesIO object from the PDF data
        pdf_file = io.BytesIO(pdf_data)
        
        # Create PDF reader
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        
        # Extract text from all pages
        text_content = ""
        for page_num in range(len(pdf_reader.pages)):
            page = pdf_reader.pages[page_num]
            text_content += page.extract_text() + "\n"
        
        logging.info(f"Successfully extracted {len(text_content)} characters from PDF")
        return text_content.strip()
        
    except Exception as e:
        logging.error(f"PDF text extraction failed: {e}", exc_info=True)
        return f"PDF processing error: {e}"

def extract_text_from_image(image_data):
    """Extract text from image data using OCR."""
    try:
        # For now, return a placeholder. In production, you'd use pytesseract or similar
        logging.info("Image OCR - using placeholder")
        return f"Image document processed. Size: {len(image_data)} bytes. [OCR text extraction would be implemented here with pytesseract or similar library]"
    except Exception as e:
        logging.error(f"Image OCR failed: {e}")
        return f"Image processing error: {e}"

# ==================== EXPENSE DATA EXTRACTION ====================

def extract_expense_data_with_gemini(file_data_bytes, attachment_url):
    """
    Uses Gemini API to extract structured expense data from document.
    """
    try:
        # Create a temporary file to save the bytes for Gemini upload
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp_invoice") as tmp_file:
            tmp_file.write(file_data_bytes)
            tmp_file_path = tmp_file.name
        
        logging.info(f"Uploading temporary file {tmp_file_path} to Gemini for expense data extraction.")
        
        # Extract text from PDF first
        logging.info("Extracting text from PDF document.")
        pdf_text = extract_text_from_pdf(file_data_bytes)
        
        # Debug: Log the extracted text (first 500 chars)
        logging.info(f"Extracted PDF text preview: {pdf_text[:500]}...")
        
        # Check if PDF extraction failed
        if "PDF processing error" in pdf_text or len(pdf_text.strip()) < 10:
            logging.error("PDF text extraction failed or returned insufficient text")
            logging.info("Falling back to regex-based extraction")
            return extract_expense_data_with_regex_fallback(file_data_bytes, attachment_url)
        
        # Use Gemini to analyze the extracted text
        logging.info("Using Gemini API to analyze extracted text.")
        
        # Create the extraction prompt
        extraction_prompt = f"""
        Analyze this invoice text and extract the following information. Return ONLY a JSON object with these exact fields:
        
        Text to analyze:
        {pdf_text}
        
        Extract and return ONLY this JSON structure:
        {{
            "expense_type": "string (e.g., Travel, Meals, Office Supplies, Professional Services, etc.)",
            "payment_type": "string (e.g., Credit Card, Cash, Check, Wire Transfer, etc.)",
            "attachment": "{attachment_url}",
            "trans_date": "string (transaction date in YYYY-MM-DD format)",
            "exp_type_desc": "string (detailed description of the expense from the invoice)",
            "payment_type_desc": "string (business, personal, etc.)",
            "trans_amount": "number (total amount from the invoice)",
            "trans_currency_code": "string (currency code like USD, EUR, etc.)",
            "billing_type": "string (type of billing - Invoice, Receipt, etc.)",
            "trans_location": "string (location where transaction occurred or vendor location)",
            "airfare_recept_nbr": "string (invoice number or receipt number)",
            "nbr_nights": "number (number of nights if hotel/accommodation, otherwise null)"
        }}
        
        If any field cannot be determined from the text, use null for that field.
        Return ONLY the JSON object, no other text.
        """
        
        # Use modern Gemini API
        try:
            # Load config to get API key
            ENV = os.getenv('ENV', 'AGENT').upper()
            l_env = importlib.import_module(f'config_{ENV}')
            api_key = l_env.GEMINI_API_KEY
            
            # Configure Gemini
            genai.configure(api_key=api_key)
            
            # Use the modern GenerativeModel
            model = genai.GenerativeModel('gemini-flash-latest')
            
            response = model.generate_content(extraction_prompt)
            response_text = response.text.strip()
            
            # Clean up the response text to ensure it's valid JSON
            if response_text.startswith('```json'):
                response_text = response_text.replace('```json', '').replace('```', '').strip()
            elif response_text.startswith('```'):
                response_text = response_text.replace('```', '').strip()
            
            extracted_data = json.loads(response_text)
            logging.info("Gemini modern API text analysis completed successfully.")
                
        except Exception as e:
            logging.error(f"Gemini modern API analysis failed: {e}")
            logging.info("Falling back to regex-based extraction")
            extracted_data = extract_expense_data_with_regex_fallback(file_data_bytes, attachment_url)
        
        # Clean up local temporary file
        os.remove(tmp_file_path)
        
        logging.info("Gemini expense data extraction completed (modern API).")
        return extracted_data
        
    except Exception as e:
        logging.error(f"Gemini expense data extraction failed: {e}", exc_info=True)
        if 'tmp_file_path' in locals() and os.path.exists(tmp_file_path):
            os.remove(tmp_file_path)
        # Fallback to regex extraction
        return extract_expense_data_with_regex_fallback(file_data_bytes, attachment_url)

def extract_expense_data_with_regex_fallback(file_data_bytes, attachment_url):
    """
    Fallback method using regex patterns to extract expense data.
    """
    logging.info("Using regex-based extraction as fallback.")
    
    # Extract text from PDF first
    try:
        pdf_file = io.BytesIO(file_data_bytes)
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        text_content = ""
        for page_num in range(len(pdf_reader.pages)):
            page = pdf_reader.pages[page_num]
            text_content += page.extract_text() + "\n"
        text_content = text_content.strip()
        logging.info(f"Extracted {len(text_content)} characters from PDF for regex processing")
    except Exception as e:
        logging.error(f"Failed to extract PDF text for regex: {e}")
        text_content = f"Binary file processed. Size: {len(file_data_bytes)} bytes"
    
    expense_data = {
        "expense_type": None,
        "payment_type": None,
        "attachment": attachment_url,
        "trans_date": None,
        "exp_type_desc": None,
        "payment_type_desc": None,
        "trans_amount": None,
        "trans_currency_code": "USD",
        "billing_type": None,
        "trans_location": None,
        "airfare_recept_nbr": None,
        "nbr_nights": None
    }
    
    # Extract amounts
    amount_patterns = [
        r'total\s*:?\s*\$?(\d+(?:\.\d{2})?)',
        r'amount\s*:?\s*\$?(\d+(?:\.\d{2})?)',
        r'price\s*:?\s*\$?(\d+(?:\.\d{2})?)',
        r'\$(\d+(?:\.\d{2})?)'
    ]
    
    for pattern in amount_patterns:
        match = re.search(pattern, text_content, re.IGNORECASE)
        if match:
            expense_data["trans_amount"] = float(match.group(1))
            break
    
    # Extract dates
    date_patterns = [
        r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'(\d{4}[/-]\d{1,2}[/-]\d{1,2})',
        r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2},?\s+\d{4}',
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, text_content, re.IGNORECASE)
        if match:
            expense_data["trans_date"] = match.group(0)
            break
    
    # Extract vendor/description
    vendor_patterns = [
        r'from\s*:?\s*([A-Za-z\s&.,]+)',
        r'vendor\s*:?\s*([A-Za-z\s&.,]+)',
        r'company\s*:?\s*([A-Za-z\s&.,]+)'
    ]
    
    for pattern in vendor_patterns:
        match = re.search(pattern, text_content, re.IGNORECASE)
        if match:
            expense_data["exp_type_desc"] = match.group(1).strip()
            break
    
    # Extract invoice/receipt number
    invoice_patterns = [
        r'invoice\s*#?\s*:?\s*([A-Z0-9-]+)',
        r'inv\s*#?\s*:?\s*([A-Z0-9-]+)',
        r'receipt\s*#?\s*:?\s*([A-Z0-9-]+)'
    ]
    
    for pattern in invoice_patterns:
        match = re.search(pattern, text_content, re.IGNORECASE)
        if match:
            expense_data["airfare_recept_nbr"] = match.group(1)
            break
    
    # Categorize expense
    expense_data["expense_type"] = categorize_expense(text_content)
    
    return expense_data

def categorize_expense(text_content):
    """Categorize expense based on keywords in the text."""
    text_lower = text_content.lower()
    
    categories = {
        "Travel": ["hotel", "flight", "airline", "taxi", "uber", "lyft", "travel", "trip"],
        "Meals": ["restaurant", "food", "meal", "dining", "lunch", "dinner", "breakfast"],
        "Office Supplies": ["office", "supplies", "stationery", "paper", "pens", "notebook"],
        "Software": ["software", "license", "subscription", "saas", "app", "tool"],
        "Transportation": ["gas", "fuel", "parking", "toll", "vehicle", "car"],
        "Communication": ["phone", "internet", "telecom", "mobile", "data"],
        "Professional Services": ["consulting", "legal", "accounting", "professional", "service"],
        "Equipment": ["computer", "laptop", "equipment", "hardware", "device"]
    }
    
    for category, keywords in categories.items():
        if any(keyword in text_lower for keyword in keywords):
            return category
    
    return "Other"

# ==================== OCI FUNCTION HANDLER ====================
def handler(ctx, data: io.BytesIO = None):
    """
    OCI Function entry point for the Extraction Agent.
    This function reads documents from OCI Object Storage and extracts structured expense data.

    Expected Input JSON:
    {
        "process_id": "unique-uuid-for-this-run",
        "attachment_object_name": "expense/emp 222/20251020/454_download_1.pdf",
        "bucket_name": "NewExpenses",
        "expense_id": "EXP-test-process-123-443",
        "from_email": "sender@example.com",
        "subject": "Invoice for emp 222"
    }

    Returns Output JSON:
    {
        "process_id": "unique-uuid-for-this-run",
        "status": "SUCCESS" or "FAILURE",
        "message": "Extraction complete",
        "extracted_data": {
            "amount": 150.00,
            "currency": "USD",
            "date": "2025-10-15",
            "vendor": "ABC Company",
            "description": "Office supplies purchase",
            "category": "Office Supplies",
            "tax_amount": 12.00,
            "total_amount": 162.00,
            "invoice_number": "INV-12345",
            "confidence_score": 0.85
        },
        "error": null
    }
    """
    logging.info("Extraction Agent invoked.")
    
    # Initialize OCI clients
    object_storage_client, namespace = init_oci_clients()
    
    response_payload = {
        "process_id": "unknown",
        "status": "FAILURE",
        "message": "An unexpected error occurred",
        "extracted_data": None,
        "error": None
    }

    try:
        body = json.loads(data.getvalue())
        process_id = body.get("process_id")
        attachment_object_name = body.get("attachment_object_name")
        bucket_name = body.get("bucket_name")
        expense_id = body.get("expense_id")
        
        if not all([process_id, attachment_object_name]):
            raise ValueError("Input JSON must contain 'process_id' and 'attachment_object_name'.")
        
        # Use bucket name from config if not provided in input
        if not bucket_name:
            bucket_name = OCI_BUCKET_NAME
        
        response_payload["process_id"] = process_id
        logging.info(f"Processing extraction for Process ID: {process_id}, Object: {attachment_object_name}")
        
        # Download the file from OCI Object Storage
        logging.info(f"Downloading file {attachment_object_name} from bucket {bucket_name}")
        response = object_storage_client.get_object(namespace, bucket_name, attachment_object_name)
        file_data = response.data.content
        
        # Create attachment URL (using the bucket name from config if not provided)
        if not bucket_name:
            bucket_name = OCI_BUCKET_NAME
        attachment_url = f"https://objectstorage.us-phoenix-1.oraclecloud.com/n/{namespace}/b/{bucket_name}/o/{attachment_object_name}"
        
        # Extract structured expense data using Gemini
        extracted_data = extract_expense_data_with_gemini(file_data, attachment_url)
        
        response_payload["extracted_data"] = extracted_data
        response_payload["status"] = "SUCCESS"
        response_payload["message"] = "Expense data extraction complete."
        
        logging.info(f"Successfully extracted data for expense {expense_id}")

    except Exception as e:
        error_msg = f"An unexpected error occurred: {e}"
        logging.error(error_msg, exc_info=True)
        response_payload["message"] = error_msg
        response_payload["error"] = error_msg
    
    return fdk_response.Response(
        ctx,
        response_data=json.dumps(response_payload),
        headers={"Content-Type": "application/json"}
    )

# ==================== LOCAL TESTING ====================
if __name__ == "__main__":
    # For local testing, create a mock context and test data
    class MockContext:
        def __init__(self):
            self.headers = {}
            self.status_code = 200
        
        def SetResponseHeaders(self, headers, status_code):
            self.headers = headers
            self.status_code = status_code
    
    # Create test data using the actual file uploaded by Reading Agent
    test_data = {
        "process_id": "test-process-123",
        "attachment_object_name": "expense/emp 222/20251020/454_download_1.pdf",
        "expense_id": "EXP-test-process-123-454",
        "from_email": "Aashray Pola <apola@beastute.com>",
        "subject": "emp 222"
    }
    
    # Convert test data to BytesIO for the handler
    test_data_bytes = io.BytesIO(json.dumps(test_data).encode())
    
    # Run the handler with mock context
    mock_ctx = MockContext()
    result = handler(mock_ctx, test_data_bytes)
    
    # Print the result for local testing
    print("Extraction Agent execution completed.")
    print(f"Status: {result.status}")
    print(f"Response data: {result.response_data}")
