from oci.addons.adk import AgentClient, Agent, tool
import imaplib, email, os, re, logging, requests, tempfile
from datetime import datetime, timedelta
import oci, importlib
from email.header import decode_header, make_header
import google.generativeai as genai
import json

# ==================== CONFIGURATION ====================
ENV = os.getenv("ENV", "STAGE").upper()
DAYS_INTERVAL = int(os.getenv("DAYS_INTERVAL", "1"))

try:
    l_env = importlib.import_module(f"config_{ENV}")
except ModuleNotFoundError:
    raise Exception(f"Configuration for environment '{ENV}' not found.")


GEMINI_API_KEY = l_env.GEMINI_API_KEY

if not GEMINI_API_KEY:
    raise Exception("❌ Missing Gemini API key. Set GEMINI_API_KEY in environment.")

genai.configure(api_key=GEMINI_API_KEY)


# Logging setup
current_date = datetime.now().strftime("%d%m%Y")
log_file = f"{l_env.LOG_PATH}-{current_date}.log"
logging.basicConfig(
    filename=log_file,
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.DEBUG,
)

# OCI setup
oci_config = oci.config.from_file(l_env.OCI_CONFIG)
object_storage = oci.object_storage.ObjectStorageClient(oci_config)
namespace = object_storage.get_namespace().data
bucket_name = l_env.OCI_BUCKET_NAME

# ==================== CLASS DEFINITIONS ====================
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
            logging.debug(f"Inserting payload: {payload}")
            response = requests.post(l_env.APEX_API_URL_EMAIL, json=payload)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to insert email details for UID {self.L_UID}: {e}")
            return False


# ==================== UTILITY FUNCTIONS ====================
def sanitize_filename(uid, original_name):
    name, ext = os.path.splitext(f"{uid}_{original_name}")
    name = re.sub(r'[^a-zA-Z0-9]+', '_', name).strip('_')[:54]
    return f"{name}{ext}"

def upload_to_oci_object_storage(object_storage, namespace, bucket_name, object_name, file_data):
    try:
        object_storage.put_object(namespace, bucket_name, object_name, file_data)
        logging.info(f'Uploaded {object_name} to OCI Object Storage in bucket {bucket_name}')
    except Exception as e:
        logging.error(f'Failed to upload {object_name}. Error: {e}')

def download_from_oci(object_storage, namespace, bucket_name, object_name):
    try:
        response = object_storage.get_object(namespace, bucket_name, object_name)
        return response.data.content
    except Exception as e:
        logging.error(f'Failed to download {object_name} from OCI: {e}')
        return None

def extract_text_with_gemini(file_path):
    """Uploads file to Gemini and extracts text using gemini-pro-vision"""
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        file_obj = genai.upload_file(path=file_path)
        response = model.generate_content(["Extract all text from this invoice.", file_obj])
        return response.text.strip()
    except Exception as e:
        logging.error(f"Gemini text extraction failed: {e}")
        return f"Error extracting text: {e}"


# ==================== MAIN TOOL ====================
@tool(description="Process unread invoice emails matching a given Employee ID, upload to OCI, and extract text using Gemini.")
def process_invoice_by_empid(emp_id: str):
    summary = {
        "emp_id": emp_id,
        "matched_emails": 0,
        "attachments_uploaded": 0,
        "errors": 0,
        "invoices": []
    }

    try:
        mail = imaplib.IMAP4_SSL(l_env.SMTP_HOST)
        mail.login(l_env.SMTP_USER, l_env.SMTP_PASSWORD)
        mail.select('inbox')
        logging.info("Connected to email server and selected inbox.")
    except Exception as e:
        logging.error(f"Email connection failed: {e}")
        return {"error": str(e)}

    try:
        since_date = (datetime.now() - timedelta(days=DAYS_INTERVAL)).strftime("%d-%b-%Y")
        result, data = mail.search(None, f'(UNSEEN SINCE "{since_date}")')

        if result != 'OK':
            logging.error("Email search failed.")
            return {"error": "Email search failed"}

        for num in data[0].split():
            result, msg_data = mail.fetch(num, '(RFC822)')
            if result != 'OK':
                summary["errors"] += 1
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            email_subject = str(make_header(decode_header(msg.get('Subject'))))
            email_from = str(make_header(decode_header(msg.get('From'))))

            # Match subject with Employee ID
            if emp_id.lower() not in email_subject.lower():
                continue

            summary["matched_emails"] += 1

            for part in msg.walk():
                if part.get_content_maintype() == 'multipart' or part.get('Content-Disposition') is None:
                    continue

                file_name = part.get_filename()
                if not file_name:
                    continue

                decoded_filename = str(make_header(decode_header(file_name)))
                unique_file_name = sanitize_filename(num.decode(), decoded_filename)
                file_data = part.get_payload(decode=True)

                # Upload to OCI
                upload_to_oci_object_storage(object_storage, namespace, bucket_name, unique_file_name, file_data)
                summary["attachments_uploaded"] += 1

                # Save locally to temp file for Gemini extraction
                with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(unique_file_name)[1]) as tmp_file:
                    tmp_file.write(file_data)
                    tmp_file_path = tmp_file.name

                invoice_text = extract_text_with_gemini(tmp_file_path)
                os.remove(tmp_file_path)

                email_record = EmailData(email_from, l_env.SMTP_USER, email_subject, unique_file_name, l_uid=num.decode())
                email_record.insert()

                summary["invoices"].append({
                    "from": email_from,
                    "subject": email_subject,
                    "filename": decoded_filename,
                    "invoice_text": invoice_text
                })

    except Exception as e:
        summary["errors"] += 1
        logging.error(f"Processing error: {e}")

    return summary


def run_reading_agent():
    """
    Initializes and runs the OCI agent.
    """
    emp_id = input("Enter EMP ID: ")

    # This outer try/except catches initialization errors
    try:
        client = AgentClient(auth_type="api_key", profile="DEFAULT", region="us-chicago-1")
        agent = Agent(
            client=client,
            agent_endpoint_id="ocid1.genaiagentendpoint.oc1.us-chicago-1.amaaaaaakjeknfqalalevd7sj5tnind2quhxbjag2ua7jgfknx3spxbijytq",
            instructions="Agent that scans emails for employee-specific invoices, uploads to OCI, and extracts text using Gemini and provide the full data as output.",
            tools=[process_invoice_by_empid]
        )

        logging.info(f"Running Reading agent for EMP ID: {emp_id}")


        try:
            emp_id = json.dumps({"emp_id": emp_id})
            #agent.setup()
            response = agent.run(emp_id,return_tool_response=True)
            agent_content = response.final_output
            return {"status": "success", "message": agent_content}

        except Exception as e:
            logging.error(f"Agent execution failed: {e}", exc_info=True)
            return {"status": "error", "message": f"Agent call failed: {str(e)}"}

    except Exception as e:
        logging.exception("Failed to initialize the OCI agent.")
        return {"status": "error", "message": f"Agent initialization failed: {str(e)}"}

if __name__ == "__main__":
    run_reading_agent()
