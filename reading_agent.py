import oci
import json
import io
import logging
import os
import re
import imaplib, email
from datetime import datetime, timedelta
from email.header import decode_header, make_header
import google.generativeai as genai
import tempfile
import requests
import importlib
from fdk import response

# ==================== LOGGING SETUP ====================
# Configure logging for OCI Functions
logging.getLogger().setLevel(logging.INFO)

# ==================== CONFIGURATION LOADING ====================
# Configuration is now expected to be passed via Function Context or environment variables.
# For local testing, you might still load from a config file.
# For OCI Functions, environment variables are preferred or values passed in the payload.

# Placeholder for configuration, will be dynamically set or come from env
# In a real OCI Function deployment, these would be Function Configuration variables.
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
OCI_BUCKET_NAME = os.getenv("OCI_BUCKET_NAME")
APEX_API_URL_EMAIL = os.getenv("APEX_API_URL_EMAIL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, OCI_BUCKET_NAME, APEX_API_URL_EMAIL, GEMINI_API_KEY]):
    logging.error("Missing one or more required environment variables for configuration.")
    # In a real deployment, this would be handled by a proper config setup.
    # For now, let's assume they are set or loaded from a config_ENV.py if running locally
    # This block would likely be in your MCP or an external orchestrator
    try:
        ENV = os.getenv("ENV", "AGENT").upper()
        l_env = importlib.import_module(f"config_{ENV}")
        SMTP_HOST = l_env.SMTP_HOST
        SMTP_USER = l_env.SMTP_USER
        SMTP_PASSWORD = l_env.SMTP_PASSWORD
        OCI_BUCKET_NAME = l_env.OCI_BUCKET_NAME
        APEX_API_URL_EMAIL = l_env.APEX_API_URL_EMAIL
        GEMINI_API_KEY = l_env.GEMINI_API_KEY
        logging.info("Configuration loaded from config_ENV.py for local testing.")
    except ModuleNotFoundError:
        logging.error("No environment variables or config_ENV.py found for local testing.")
        # Re-raise if essential config is still missing
        if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, OCI_BUCKET_NAME, APEX_API_URL_EMAIL, GEMINI_API_KEY]):
            raise Exception("Essential configuration is missing. Cannot proceed.")


genai.configure(api_key=GEMINI_API_KEY)

# ==================== OCI CLIENTS ====================

# OCI Object Storage Client (will use Resource Principal by default in OCI Functions)
object_storage_client = None
namespace = None

def init_oci_clients():
    global object_storage_client, namespace
    if object_storage_client is None:
        try:
            signer = oci.auth.signers.get_resource_principals_signer()
            logging.info("Authenticated using Resource Principal for OCI Object Storage.")
            config_for_rp = {"region": signer.region} # RP signer sets region automatically
            object_storage_client = oci.object_storage.ObjectStorageClient(config_for_rp, signer=signer)
        except Exception:
            logging.warning("Resource Principal not available. Falling back to default OCI config for Object Storage (local testing).")
            oci_config = oci.config.from_file(
                oci.config.DEFAULT_LOCATION,
                oci.config.DEFAULT_PROFILE
            )
            object_storage_client = oci.object_storage.ObjectStorageClient(oci_config)
        namespace = object_storage_client.get_namespace().data
    return object_storage_client, namespace

# ==================== CLASS DEFINITIONS ====================
# EmailData class - unchanged, as it defines your data structure
class EmailData:
    def __init__(self, from_email, to_email, subject, attachment_name, odu_doc_id="0", l_uid="0"):
        self.FROM_EMAIL = from_email
        self.TO_EMAIL = to_email
        self.SUBJECT_LINE = subject
        self.ATTACHMENT_NAMES = attachment_name
        self.ODU_DOC_ID = odu_doc_id
        self.PROCESSED = 'N'
        self.L_UID = l_uid

    def insert(self):
        payload = {
            "FROM_EMAIL": self.FROM_EMAIL,
            "TO_EMAIL": self.TO_EMAIL,
            "SUBJECT_LINE": self.SUBJECT_LINE,
            "ATTACHMENT_NAMES": self.ATTACHMENT_NAMES,
            "ODU_DOC_ID": self.ODU_DOC_ID,
            "PROCESSED": self.PROCESSED,
            "L_UID": self.L_UID
        }
        try:
            logging.debug(f"Inserting payload to APEX: {payload}")
            response = requests.post(APEX_API_URL_EMAIL, json=payload)
            response.raise_for_status()
            logging.info(f"Successfully inserted email details for UID {self.L_UID} into APEX.")
            return True
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to insert email details for UID {self.L_UID} into APEX: {e}", exc_info=True)
            return False

# ==================== UTILITY FUNCTIONS ====================
def sanitize_filename(uid, original_name):
    name, ext = os.path.splitext(f"{uid}_{original_name}")
    name = re.sub(r'[^a-zA-Z0-9]+', '_', name).strip('_')[:54]
    return f"{name}{ext}"

def upload_to_oci_object_storage(obj_storage_client, namespace, bucket_name, object_name, file_data):
    try:
        obj_storage_client.put_object(namespace, bucket_name, object_name, file_data)
        logging.info(f'Uploaded {object_name} to OCI Object Storage in bucket {bucket_name}')
        return True
    except Exception as e:
        logging.error(f'Failed to upload {object_name}. Error: {e}', exc_info=True)
        return False

def extract_text_with_gemini(file_data_bytes):
    """Uploads file data (bytes) to Gemini and extracts text using gemini-pro-vision."""
    try:
        # Create a temporary file to save the bytes for Gemini upload
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp_invoice") as tmp_file:
            tmp_file.write(file_data_bytes)
            tmp_file_path = tmp_file.name
        
        logging.info(f"Uploading temporary file {tmp_file_path} to Gemini for text extraction.")
        model = genai.GenerativeModel("gemini-1.5-flash") # Use 1.5-flash or 1.5-pro for better performance/capabilities
        file_obj = genai.upload_file(path=tmp_file_path, display_name=os.path.basename(tmp_file_path)) # Add display_name
        
        # Ensure the file is actually uploaded before use
        while file_obj.state.name == "PROCESSING":
            logging.info("Gemini file upload processing...")
            import time
            time.sleep(1)
            file_obj = genai.get_file(file_obj.name)

        logging.info(f"Gemini file {file_obj.name} uploaded. State: {file_obj.state.name}")
        
        # Changed prompt for better text extraction (Gemini handles images well)
        response = model.generate_content([
            "Extract all visible text from this document, including any tables or key-value pairs.",
            file_obj
        ])
        
        # Delete the file from Gemini if no longer needed
        genai.delete_file(file_obj.name)
        
        # Clean up local temporary file
        os.remove(tmp_file_path)

        logging.info("Gemini text extraction successful.")
        return response.text.strip()
    except Exception as e:
        logging.error(f"Gemini text extraction failed: {e}", exc_info=True)
        if 'tmp_file_path' in locals() and os.path.exists(tmp_file_path):
            os.remove(tmp_file_path) # Ensure cleanup on error
        return f"Error extracting text with Gemini: {e}"


# ==================== OCI FUNCTION HANDLER ====================
def handler(ctx, data: io.BytesIO = None):
    """
    OCI Function entry point for the Ingestion & Reading Agent.
    This function processes emails, uploads attachments to OCI Object Storage,
    and extracts text using Gemini.

    Expected Input JSON from MCP:
    {
        "process_id": "unique-uuid-for-this-run", # MCP's unique identifier for this agent invocation
        "emp_id": "E12345",
        "days_interval": 1, # Optional: number of days to search back
        "mark_as_read": true # Optional: whether to mark emails as read after processing
    }

    Returns Output JSON to MCP:
    {
        "process_id": "unique-uuid-for-this-run",
        "status": "SUCCESS" or "FAILURE",
        "message": "Summary message",
        "invoices_processed": [
            {
                "expense_id": "GUID_FOR_THIS_INVOICE_GENERATED_HERE",
                "from_email": "sender@example.com",
                "subject": "Invoice for E12345",
                "attachment_object_name": "E12345_invoice_123.pdf", # Name in Object Storage
                "extracted_text": "Full text from Gemini",
                "apex_db_record_success": true,
                "error": null
            },
            ...
        ],
        "errors": []
    }
    """
    logging.info("Ingestion & Reading Agent invoked.")
    
    # Initialize OCI clients first
    obj_storage_client, ns = init_oci_clients()

    response_payload = {
        "process_id": "unknown",
        "status": "FAILURE",
        "message": "An unexpected error occurred",
        "invoices_processed": [],
        "errors": []
    }

    try:
        body = json.loads(data.getvalue())
        process_id = body.get("process_id")
        emp_id = body.get("emp_id")
        days_interval = body.get("days_interval", 1) # Default to 1 day
        mark_as_read = body.get("mark_as_read", True) # Default to mark as read

        if not process_id or not emp_id:
            raise ValueError("Input JSON must contain 'process_id' and 'emp_id'.")
        
        response_payload["process_id"] = process_id
        logging.info(f"Processing request for Process ID: {process_id}, Employee ID: {emp_id}")

        mail = imaplib.IMAP4_SSL(SMTP_HOST)
        mail.login(SMTP_USER, SMTP_PASSWORD)
        mail.select('inbox')
        logging.info("Connected to email server and selected inbox.")

        since_date = (datetime.now() - timedelta(days=days_interval)).strftime("%d-%b-%Y")
        
        # Use UNSEEN to process only new emails. If you want to re-process, use ALL.
        # Ensure to mark as read if UNSEEN is used to avoid repeated processing.
        search_criteria = f'(UNSEEN SINCE "{since_date}")' 
        logging.info(f"Searching emails with criteria: {search_criteria}")
        result, data = mail.search(None, search_criteria)

        if result != 'OK':
            raise Exception("Email search failed.")

        uids_to_mark_read = []
        for num_bytes in data[0].split():
            uid = num_bytes.decode() # UID is a string
            
            # Fetch the email by UID
            # (RFC822) fetches the entire message including headers and body
            result, msg_data = mail.fetch(uid, '(RFC822)')
            if result != 'OK':
                logging.error(f"Failed to fetch email UID {uid}. Skipping.")
                response_payload["errors"].append(f"Failed to fetch email UID {uid}.")
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            email_subject = str(make_header(decode_header(msg.get('Subject', 'No Subject'))))
            email_from = str(make_header(decode_header(msg.get('From', 'No Sender'))))
            
            logging.info(f"Processing email from '{email_from}' with subject '{email_subject}' (UID: {uid})")

            # Match subject with Employee ID
            if emp_id.lower() not in email_subject.lower():
                logging.info(f"Subject '{email_subject}' does not contain EMP ID '{emp_id}'. Skipping.")
                continue

            processed_invoice_data = {
                "expense_id": f"EXP-{process_id}-{uid}", # Generate a unique expense_id here
                "from_email": email_from,
                "subject": email_subject,
                "attachment_object_name": None,
                "extracted_text": None,
                "apex_db_record_success": False,
                "error": None
            }
            
            attachments_found_for_email = False
            for part in msg.walk():
                # Skip non-attachment parts (like plain text body or html body)
                if not part.get_filename() or part.get_content_maintype() == 'multipart':
                    continue
                
                # Check for Content-Disposition header for attachments
                if part.get('Content-Disposition') is None or not part.get('Content-Disposition').startswith('attachment'):
                    continue

                attachments_found_for_email = True
                file_name = part.get_filename()
                decoded_filename = str(make_header(decode_header(file_name)))
                
                # Generate unique object name for OCI
                # Ensure object name is unique and includes relevant identifiers for tracing
                object_name_prefix = f"expense/{emp_id}/{datetime.now().strftime('%Y%m%d')}/"
                unique_object_name = object_name_prefix + sanitize_filename(uid, decoded_filename)
                file_data = part.get_payload(decode=True)

                # 1. Upload to OCI Object Storage
                upload_success = upload_to_oci_object_storage(obj_storage_client, ns, OCI_BUCKET_NAME, unique_object_name, file_data)
                
                if upload_success:
                    processed_invoice_data["attachment_object_name"] = unique_object_name
                    
                    # 2. Extract text with Gemini
                    invoice_text = extract_text_with_gemini(file_data)
                    processed_invoice_data["extracted_text"] = invoice_text
                    
                    # 3. Insert metadata into APEX DB
                    email_record = EmailData(email_from, SMTP_USER, email_subject, unique_object_name, l_uid=uid)
                    processed_invoice_data["apex_db_record_success"] = email_record.insert()
                    
                    # If everything for this attachment is successful, mark email for read
                    uids_to_mark_read.append(uid)
                else:
                    processed_invoice_data["error"] = "Failed to upload attachment to OCI Object Storage."

                # Add this processed invoice to the list
                response_payload["invoices_processed"].append(processed_invoice_data)
                
            if not attachments_found_for_email:
                logging.info(f"No valid attachments found for email UID {uid}. Skipping.")

        # Mark emails as read if requested and successfully processed
        if mark_as_read and uids_to_mark_read:
            uids_str = ",".join(uids_to_mark_read)
            mail.store(uids_str, '+FLAGS', '\\Seen')
            logging.info(f"Marked UIDs {uids_to_mark_read} as \\Seen.")

        mail.logout()
        response_payload["status"] = "SUCCESS"
        response_payload["message"] = "Email processing and text extraction complete."

    except (imaplib.IMAP4.error, imaplib.IMAP4_SSL.error) as e:
        error_msg = f"Email server error: {e}"
        logging.error(error_msg, exc_info=True)
        response_payload["message"] = error_msg
        response_payload["errors"].append(error_msg)
    except Exception as e:
        error_msg = f"An unexpected error occurred: {e}"
        logging.error(error_msg, exc_info=True)
        response_payload["message"] = error_msg
        response_payload["errors"].append(error_msg)
    
    return response.Response(
        ctx,
        response_data=json.dumps(response_payload),
        headers={"Content-Type": "application/json"}
    )