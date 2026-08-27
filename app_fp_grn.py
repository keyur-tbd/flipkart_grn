import os
import json
import base64
import tempfile
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from io import BytesIO
import schedule
import re

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# === NEW LLAMA CLOUD SDK ===
try:
    from llama_cloud import LlamaCloud
    LLAMA_AVAILABLE = True
except ImportError:
    LLAMA_AVAILABLE = False

# Custom SUCCESS log level
logging.SUCCESS = 25
logging.addLevelName(logging.SUCCESS, "SUCCESS")

def success(self, message, *args, **kwargs):
    if self.isEnabledFor(logging.SUCCESS):
        self._log(logging.SUCCESS, message, args, **kwargs)

logging.Logger.success = success

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Hardcoded configuration
CONFIG = {
    'gmail': {
        'sender': "prrfreshproducepvtltd@gmail.com",
        'search_term': "Invoice, Tax Invoice",
        'days_back': 2,
        'max_results': 100,
        'gdrive_folder_id': "1yNPbQRiqakoKgQhwbX1cUV0EmA80CjjR"
    },
    'pdf': {
        'drive_folder_id': "1ETcHsnCFcKeccwPkBs6rUEcI845fhbtm",
        'llama_api_key': "llx-BXl9kRSflD8negSBsSmBgspX7fLrb8IDj29suOeMsCLXYaR0",
        'llama_agent': "Flipkart PRR GRN",
        'spreadsheet_id': "1DNowuKF1gk0AVu2Ytt1wKZqCtzllhPIZTAwk5p7aCNQ",
        'sheet_range': "pprgrn",
        'days_back': 2,
        'max_files': 100
    },
    'log': {
        'sheet_range': "workflow_logs_pprgrn"
    }
}

TOKEN_FILE = 'token.json'
MAX_RETRIES = 3
RETRY_WAIT = 2

# Static, ordered column headers for the Invoice sheet — never changes regardless of extracted data
STATIC_POS_HEADERS = [
    # Invoice header
    "invoice_number", "invoice_date", "supplier_invoice_number", "supplier_invoice_date", "other_references",
    # Customer details
    "customer_name", "customer_address", "customer_gstin_uin", "customer_state_name", "customer_state_code",
    # Supplier details
    "supplier_name", "supplier_address", "supplier_gstin_uin", "supplier_state_name", "supplier_state_code",
    # Line item fields
    "sl_no", "description_of_goods", "quantity", "unit", "rate", "amount",
    # Summary fields
    "sub_total_amount", "total_quantity_of_items", "total_amount_payable", "amount_chargeable_in_words",
    # Deduction fields (flattened from summary.deductions array)
    "deduction_description", "deduction_percentage", "deduction_amount",
    # Footer details
    "company_gstin_uin_at_footer", "company_for_authorized_signatory",
    # Metadata
    "source_file", "drive_file_id", "processed_date",
]


class MilkbasketAutomation:
    def __init__(self):
        self.gmail_service = None
        self.drive_service = None
        self.sheets_service = None
        
        self.gmail_scopes = ['https://www.googleapis.com/auth/gmail.readonly']
        self.drive_scopes = ['https://www.googleapis.com/auth/drive.file']
        self.sheets_scopes = ['https://www.googleapis.com/auth/spreadsheets']
        
        self.logs = []
        self.current_stats = {
            'gmail': {'processed_emails': 0, 'total_attachments': 0, 'successful_uploads': 0, 'failed_uploads': 0},
            'pdf': {'total_pdfs': 0, 'processed_pdfs': 0, 'skipped_pdfs': 0, 'failed_pdfs': 0, 'rows_added': 0}
        }
    
    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = {"timestamp": timestamp, "level": level.upper(), "message": message}
        self.logs.append(log_entry)
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]
        level_map = {"INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR, "SUCCESS": logging.SUCCESS}
        logger.log(level_map.get(level.upper(), logging.INFO), message)
    
    def get_logs(self): return self.logs
    def clear_logs(self): self.logs = []
    def reset_stats(self):
        self.current_stats = {
            'gmail': {'processed_emails': 0, 'total_attachments': 0, 'successful_uploads': 0, 'failed_uploads': 0},
            'pdf': {'total_pdfs': 0, 'processed_pdfs': 0, 'skipped_pdfs': 0, 'failed_pdfs': 0, 'rows_added': 0}
        }
    def get_stats(self): return self.current_stats

    def authenticate(self):
        try:
            self.log("Starting authentication process...", "INFO")
            combined_scopes = list(set(self.gmail_scopes + self.drive_scopes + self.sheets_scopes))
            
            creds = None
            if os.path.exists(TOKEN_FILE):
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, combined_scopes)
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as token:
                        token.write(creds.to_json())
            
            if not creds or not creds.valid:
                if not os.path.exists('credentials.json'):
                    self.log("credentials.json not found.", "ERROR")
                    return False
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', combined_scopes)
                creds = flow.run_local_server(port=0)
                with open(TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
                self.log("OAuth authentication successful!", "SUCCESS")
            
            self.gmail_service = build('gmail', 'v1', credentials=creds)
            self.drive_service = build('drive', 'v3', credentials=creds)
            self.sheets_service = build('sheets', 'v4', credentials=creds)
            return True
        except Exception as e:
            self.log(f"Authentication failed: {str(e)}", "ERROR")
            return False

    def retry_wrapper(self, func, *args, **kwargs):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                self.log(f"Attempt {attempt} failed: {str(e)}", "WARNING")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_WAIT)
                else:
                    raise

    def search_emails(self, sender: str = "", search_term: str = "", days_back: int = 7, max_results: int = 50) -> List[Dict]:
        def _search():
            query_parts = ["has:attachment"]
            if sender:
                query_parts.append(f'from:"{sender}"')
            if search_term:
                if "," in search_term:
                    keywords = [k.strip() for k in search_term.split(",")]
                    keyword_query = " OR ".join([f'"{k}"' for k in keywords if k])
                    if keyword_query:
                        query_parts.append(f"({keyword_query})")
                else:
                    query_parts.append(f'"{search_term}"')
            start_date = datetime.now() - timedelta(days=days_back)
            query_parts.append(f"after:{start_date.strftime('%Y/%m/%d')}")
            query = " ".join(query_parts)
            self.log(f"Searching Gmail with query: {query}", "INFO")
            result = self.gmail_service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
            messages = result.get('messages', [])
            self.log(f"Gmail search returned {len(messages)} messages", "INFO")
            return messages
        try:
            return self.retry_wrapper(_search)
        except Exception as e:
            self.log(f"Email search failed: {str(e)}", "ERROR")
            return []

    def get_existing_filenames(self, spreadsheet_id: str, sheet_range: str) -> set:
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=sheet_range, majorDimension="ROWS"
            ).execute()
            values = result.get('values', [])
            if not values:
                return set()
            headers = values[0]
            if "source_file" not in headers:
                return set()
            id_index = headers.index("source_file")
            existing_ids = {row[id_index] for row in values[1:] if len(row) > id_index and row[id_index]}
            self.log(f"Found {len(existing_ids)} existing file names in sheet", "INFO")
            return existing_ids
        except Exception as e:
            self.log(f"Failed to get existing file names: {str(e)}", "ERROR")
            return set()

    def get_existing_file_ids(self, spreadsheet_id: str, sheet_range: str) -> set:
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=sheet_range, majorDimension="ROWS"
            ).execute()
            values = result.get('values', [])
            if not values:
                return set()
            headers = values[0]
            if "drive_file_id" not in headers:
                return set()
            id_index = headers.index("drive_file_id")
            existing_ids = {row[id_index] for row in values[1:] if len(row) > id_index and row[id_index]}
            self.log(f"Found {len(existing_ids)} existing file IDs in sheet", "INFO")
            return existing_ids
        except Exception as e:
            self.log(f"Failed to get existing file IDs: {str(e)}", "ERROR")
            return set()

    def process_gmail_workflow(self, config: dict, progress_callback=None, status_callback=None):
        try:
            if status_callback: status_callback("Starting Gmail workflow...")
            self.log("Starting Gmail workflow", "INFO")
            if progress_callback: progress_callback(10)

            emails = self.search_emails(
                sender=config['sender'],
                search_term=config['search_term'],
                days_back=config['days_back'],
                max_results=config['max_results']
            )
            self.current_stats['gmail']['processed_emails'] = len(emails)

            if progress_callback: progress_callback(25)
            if not emails:
                self.log("No emails found matching criteria", "WARNING")
                return {'success': True, 'processed': 0}

            if status_callback: status_callback(f"Found {len(emails)} emails. Processing attachments...")
            self.log(f"Found {len(emails)} emails matching criteria", "INFO")

            base_folder_name = "Gmail_Attachments"
            base_folder_id = self._create_drive_folder(base_folder_name, config.get('gdrive_folder_id'))

            if not base_folder_id:
                self.log("Failed to create base folder in Google Drive", "ERROR")
                return {'success': False, 'processed': 0}

            if progress_callback: progress_callback(50)

            processed_count = 0
            total_attachments = 0

            for i, email in enumerate(emails):
                try:
                    if status_callback: status_callback(f"Processing email {i+1}/{len(emails)}")
                    email_details = self._get_email_details(email['id'])
                    subject = email_details.get('subject', 'No Subject')[:50]

                    inv_match = re.search(r'against Inv: (\S+)', subject)
                    if inv_match and '/' in inv_match.group(1):
                        self.log(f"Skipping email (contains '/'): {subject}", "INFO")
                        continue

                    self.log(f"Processing email: {subject}", "INFO")

                    message = self.gmail_service.users().messages().get(userId='me', id=email['id'], format='full').execute()
                    attachment_count = self._extract_attachments_from_email(email['id'], message['payload'], config, base_folder_id)

                    total_attachments += attachment_count
                    self.current_stats['gmail']['total_attachments'] += attachment_count
                    self.current_stats['gmail']['successful_uploads'] += attachment_count

                    if attachment_count > 0:
                        processed_count += 1
                        self.log(f"Found {attachment_count} attachments", "SUCCESS")
                    
                    if progress_callback:
                        progress = 50 + (i + 1) / len(emails) * 45
                        progress_callback(int(progress))
                except Exception as e:
                    self.log(f"Failed to process email: {str(e)}", "ERROR")
                    self.current_stats['gmail']['failed_uploads'] += 1

            if progress_callback: progress_callback(100)
            if status_callback: status_callback(f"Gmail workflow completed! Processed {total_attachments} attachments")
            self.log(f"Gmail workflow completed! Processed {total_attachments} attachments from {processed_count} emails", "SUCCESS")
            return {'success': True, 'processed': total_attachments}
        except Exception as e:
            self.log(f"Gmail workflow failed: {str(e)}", "ERROR")
            return {'success': False, 'processed': 0}

    def _get_email_details(self, message_id: str) -> Dict:
        try:
            message = self.gmail_service.users().messages().get(userId='me', id=message_id, format='metadata').execute()
            headers = message['payload'].get('headers', [])
            return {
                'id': message_id,
                'sender': next((h['value'] for h in headers if h['name'] == "From"), "Unknown"),
                'subject': next((h['value'] for h in headers if h['name'] == "Subject"), "(No Subject)"),
                'date': next((h['value'] for h in headers if h['name'] == "Date"), "")
            }
        except Exception as e:
            self.log(f"Failed to get email details: {str(e)}", "ERROR")
            return {'id': message_id, 'sender': 'Unknown', 'subject': 'Unknown', 'date': ''}

    def _create_drive_folder(self, folder_name: str, parent_folder_id: Optional[str] = None) -> str:
        try:
            query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            if parent_folder_id:
                query += f" and '{parent_folder_id}' in parents"
            existing = self.drive_service.files().list(q=query, fields='files(id, name)').execute()
            if existing.get('files'):
                return existing['files'][0]['id']
            
            folder_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
            if parent_folder_id:
                folder_metadata['parents'] = [parent_folder_id]
            folder = self.drive_service.files().create(body=folder_metadata, fields='id').execute()
            return folder.get('id')
        except Exception as e:
            self.log(f"Failed to create folder {folder_name}: {str(e)}", "ERROR")
            return ""

    def _extract_attachments_from_email(self, message_id: str, payload: Dict, config: dict, base_folder_id: str) -> int:
        processed_count = 0
        if "parts" in payload:
            for part in payload["parts"]:
                processed_count += self._extract_attachments_from_email(message_id, part, config, base_folder_id)
        elif payload.get("filename") and "attachmentId" in payload.get("body", {}):
            filename = payload.get("filename", "")
            try:
                attachment_id = payload["body"].get("attachmentId")
                att = self.gmail_service.users().messages().attachments().get(userId='me', messageId=message_id, id=attachment_id).execute()
                file_data = base64.urlsafe_b64decode(att["data"].encode("UTF-8"))

                search_folder_id = self._create_drive_folder(config.get('search_term', 'all-attachments'), base_folder_id)
                type_folder_id = self._create_drive_folder(self._classify_extension(filename), search_folder_id)

                clean_filename = self._sanitize_filename(filename)
                if not self._file_exists_in_folder(clean_filename, type_folder_id):
                    file_metadata = {'name': clean_filename, 'parents': [type_folder_id]}
                    media = MediaIoBaseUpload(BytesIO(file_data), mimetype='application/octet-stream', resumable=True)
                    self.drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                    self.log(f"Uploaded: {clean_filename}", "INFO")
                    processed_count = 1
                else:
                    self.log(f"File already exists, skipping: {clean_filename}", "INFO")
            except Exception as e:
                self.log(f"Failed to process attachment {filename}: {str(e)}", "ERROR")
        return processed_count

    def _sanitize_filename(self, filename: str) -> str:
        cleaned = re.sub(r'[<>:"/\|?*]', '_', filename)
        if len(cleaned) > 100:
            name_parts = cleaned.split('.')
            if len(name_parts) > 1:
                cleaned = f"{'.'.join(name_parts[:-1])[:95]}.{name_parts[-1]}"
            else:
                cleaned = cleaned[:100]
        return cleaned

    def _classify_extension(self, filename: str) -> str:
        if not filename or '.' not in filename:
            return "Other"
        ext = filename.split(".")[-1].lower()
        type_map = {
            "pdf": "PDFs", "doc": "Documents", "docx": "Documents", "txt": "Documents",
            "xls": "Spreadsheets", "xlsx": "Spreadsheets", "csv": "Spreadsheets",
            "jpg": "Images", "jpeg": "Images", "png": "Images", "gif": "Images",
            "ppt": "Presentations", "pptx": "Presentations",
            "zip": "Archives", "rar": "Archives", "7z": "Archives",
        }
        return type_map.get(ext, "Other")

    def _file_exists_in_folder(self, filename: str, folder_id: str) -> bool:
        try:
            query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
            files = self.drive_service.files().list(q=query, fields='files(id, name)').execute().get('files', [])
            return len(files) > 0
        except:
            return False

    # ====================== UPDATED PDF WORKFLOW (FIXED) ======================
    def process_pdf_workflow(self, config: dict, progress_callback=None, status_callback=None):
        try:
            if not LLAMA_AVAILABLE:
                self.log("llama-cloud not installed. Run: pip install llama-cloud>=2.1", "ERROR")
                return {'success': False, 'processed': 0}

            if status_callback: status_callback("Starting PDF processing workflow...")
            self.log("Starting PDF processing workflow with llama-cloud SDK...", "INFO")
            if progress_callback: progress_callback(20)

            client = LlamaCloud(api_key=config['llama_api_key'])

            if progress_callback: progress_callback(40)

            # Always fetch existing file IDs and filenames to prevent any duplication
            existing_file_ids = self.get_existing_file_ids(config['spreadsheet_id'], config['sheet_range'])
            existing_file_names = self.get_existing_filenames(config['spreadsheet_id'], config['sheet_range'])
            pdf_files = self._list_drive_files(config['drive_folder_id'], config['days_back'])
            self.current_stats['pdf']['total_pdfs'] = len(pdf_files)

            skipped = []
            files_to_process = []
            for f in pdf_files:
                if f['id'] in existing_file_ids:
                    self.log(f"Skipping (drive_file_id already in sheet): {f['name']}", "INFO")
                    skipped.append(f)
                elif f['name'] in existing_file_names:
                    self.log(f"Skipping (filename already in sheet): {f['name']}", "INFO")
                    skipped.append(f)
                else:
                    files_to_process.append(f)

            self.current_stats['pdf']['skipped_pdfs'] += len(skipped)
            files_to_process = files_to_process[:config.get('max_files', len(files_to_process))]

            if not files_to_process:
                self.log("No PDF files to process", "WARNING")
                return {'success': True, 'processed': 0}

            self.log(f"Processing {len(files_to_process)} PDFs using agent '{config['llama_agent']}'", "INFO")

            processed_count = 0
            total_rows = 0
            sheet_name = config['sheet_range'].split('!')[0]

            for i, file in enumerate(files_to_process):
                try:
                    if status_callback: status_callback(f"Processing PDF {i+1}/{len(files_to_process)}: {file['name']}")
                    self.log(f"Processing: {file['name']}", "INFO")

                    pdf_data = self._download_from_drive(file['id'], file['name'])
                    if not pdf_data:
                        self.current_stats['pdf']['failed_pdfs'] += 1
                        continue

                    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_file:
                        temp_file.write(pdf_data)
                        temp_path = temp_file.name

                    extracted_data = None
                    try:
                        # Upload file
                        with open(temp_path, "rb") as f:
                            file_obj = client.files.create(file=f, purpose="extract")
                        file_id = file_obj.id

                        # Create extraction job with the Flipkart Invoice agent
                        job = client.extract.create(
                            file_input=file_id,
                            configuration={
                                "agent_name": config['llama_agent'],
                                "extraction_target": "per_doc",
                                "tier": "agentic",
                                "data_schema": {
                                    "type": "object",
                                    "properties": {
                                        "invoice_header": {
                                            "description": "General information about the invoice.",
                                            "type": "object",
                                            "properties": {
                                                "invoice_number": {
                                                    "description": "The unique identifier for the invoice.",
                                                    "type": "string"
                                                },
                                                "invoice_date": {
                                                    "description": "The date when the invoice was issued.",
                                                    "type": "string"
                                                },
                                                "supplier_invoice_number": {
                                                    "description": "The invoice number as provided by the supplier, if different from the main invoice number.",
                                                    "anyOf": [
                                                        {
                                                            "description": "The invoice number as provided by the supplier, if different from the main invoice number.",
                                                            "type": "string"
                                                        },
                                                        {"type": "null"}
                                                    ]
                                                },
                                                "supplier_invoice_date": {
                                                    "description": "The date associated with the supplier's invoice number.",
                                                    "anyOf": [
                                                        {
                                                            "description": "The date associated with the supplier's invoice number.",
                                                            "type": "string"
                                                        },
                                                        {"type": "null"}
                                                    ]
                                                },
                                                "other_references": {
                                                    "description": "Any other reference numbers or details mentioned in the invoice header.",
                                                    "anyOf": [
                                                        {
                                                            "description": "Any other reference numbers or details mentioned in the invoice header.",
                                                            "type": "string"
                                                        },
                                                        {"type": "null"}
                                                    ]
                                                }
                                            },
                                            "required": [
                                                "invoice_number", "invoice_date", "supplier_invoice_number",
                                                "supplier_invoice_date", "other_references"
                                            ],
                                            "additionalProperties": False
                                        },
                                        "customer_details": {
                                            "description": "Details of the customer (Bill To / Consignee).",
                                            "type": "object",
                                            "properties": {
                                                "name": {
                                                    "description": "The name of the customer or consignee.",
                                                    "type": "string"
                                                },
                                                "address": {
                                                    "description": "The full address of the customer or consignee.",
                                                    "type": "string"
                                                },
                                                "gstin_uin": {
                                                    "description": "The GSTIN/UIN (Goods and Services Tax Identification Number / Unique Identification Number) of the customer.",
                                                    "type": "string"
                                                },
                                                "state_name": {
                                                    "description": "The state name of the customer's location.",
                                                    "anyOf": [
                                                        {
                                                            "description": "The state name of the customer's location.",
                                                            "type": "string"
                                                        },
                                                        {"type": "null"}
                                                    ]
                                                },
                                                "state_code": {
                                                    "description": "The state code of the customer's location.",
                                                    "anyOf": [
                                                        {
                                                            "description": "The state code of the customer's location.",
                                                            "type": "string"
                                                        },
                                                        {"type": "null"}
                                                    ]
                                                }
                                            },
                                            "required": ["name", "address", "gstin_uin", "state_name", "state_code"],
                                            "additionalProperties": False
                                        },
                                        "supplier_details": {
                                            "description": "Details of the supplier (Bill From).",
                                            "type": "object",
                                            "properties": {
                                                "name": {
                                                    "description": "The name of the supplier.",
                                                    "type": "string"
                                                },
                                                "address": {
                                                    "description": "The full address of the supplier.",
                                                    "type": "string"
                                                },
                                                "gstin_uin": {
                                                    "description": "The GSTIN/UIN (Goods and Services Tax Identification Number / Unique Identification Number) of the supplier.",
                                                    "type": "string"
                                                },
                                                "state_name": {
                                                    "description": "The state name of the supplier's location.",
                                                    "anyOf": [
                                                        {
                                                            "description": "The state name of the supplier's location.",
                                                            "type": "string"
                                                        },
                                                        {"type": "null"}
                                                    ]
                                                },
                                                "state_code": {
                                                    "description": "The state code of the supplier's location.",
                                                    "anyOf": [
                                                        {
                                                            "description": "The state code of the supplier's location.",
                                                            "type": "string"
                                                        },
                                                        {"type": "null"}
                                                    ]
                                                }
                                            },
                                            "required": ["name", "address", "gstin_uin", "state_name", "state_code"],
                                            "additionalProperties": False
                                        },
                                        "line_items": {
                                            "description": "A list of individual goods or services provided.",
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "sl_no": {
                                                        "description": "The serial number of the line item.",
                                                        "anyOf": [
                                                            {
                                                                "description": "The serial number of the line item.",
                                                                "type": "number"
                                                            },
                                                            {"type": "null"}
                                                        ]
                                                    },
                                                    "description_of_goods": {
                                                        "description": "A detailed description of the goods or service.",
                                                        "type": "string"
                                                    },
                                                    "quantity": {
                                                        "description": "The quantity of the item.",
                                                        "type": "number"
                                                    },
                                                    "unit": {
                                                        "description": "The unit of measurement for the quantity (e.g., 'pcs', 'kg').",
                                                        "anyOf": [
                                                            {
                                                                "description": "The unit of measurement for the quantity (e.g., 'pcs', 'kg').",
                                                                "type": "string"
                                                            },
                                                            {"type": "null"}
                                                        ]
                                                    },
                                                    "rate": {
                                                        "description": "The unit price of the item.",
                                                        "type": "number"
                                                    },
                                                    "amount": {
                                                        "description": "The total amount for this line item (quantity * rate).",
                                                        "type": "number"
                                                    }
                                                },
                                                "required": ["sl_no", "description_of_goods", "quantity", "unit", "rate", "amount"],
                                                "additionalProperties": False
                                            }
                                        },
                                        "summary": {
                                            "description": "Summary of charges and totals for the invoice.",
                                            "type": "object",
                                            "properties": {
                                                "sub_total_amount": {
                                                    "description": "The total amount of all line items before any deductions or taxes.",
                                                    "type": "number"
                                                },
                                                "deductions": {
                                                    "description": "A list of deductions applied to the invoice.",
                                                    "anyOf": [
                                                        {
                                                            "description": "A list of deductions applied to the invoice.",
                                                            "type": "array",
                                                            "items": {
                                                                "type": "object",
                                                                "properties": {
                                                                    "description": {
                                                                        "description": "Description of the deduction (e.g., 'TDS 194Q').",
                                                                        "type": "string"
                                                                    },
                                                                    "percentage": {
                                                                        "description": "The percentage of the deduction, if applicable.",
                                                                        "anyOf": [
                                                                            {
                                                                                "description": "The percentage of the deduction, if applicable.",
                                                                                "type": "number"
                                                                            },
                                                                            {"type": "null"}
                                                                        ]
                                                                    },
                                                                    "amount": {
                                                                        "description": "The monetary amount of the deduction.",
                                                                        "type": "number"
                                                                    }
                                                                },
                                                                "required": ["description", "percentage", "amount"],
                                                                "additionalProperties": False
                                                            }
                                                        },
                                                        {"type": "null"}
                                                    ]
                                                },
                                                "total_quantity_of_items": {
                                                    "description": "The sum of quantities of all items in the invoice.",
                                                    "anyOf": [
                                                        {
                                                            "description": "The sum of quantities of all items in the invoice.",
                                                            "type": "number"
                                                        },
                                                        {"type": "null"}
                                                    ]
                                                },
                                                "total_amount_payable": {
                                                    "description": "The final amount to be paid after all calculations, before tax if any.",
                                                    "type": "number"
                                                },
                                                "amount_chargeable_in_words": {
                                                    "description": "The total amount payable written out in words.",
                                                    "type": "string"
                                                }
                                            },
                                            "required": [
                                                "sub_total_amount", "deductions", "total_quantity_of_items",
                                                "total_amount_payable", "amount_chargeable_in_words"
                                            ],
                                            "additionalProperties": False
                                        },
                                        "footer_details": {
                                            "description": "Information typically found at the bottom of the invoice.",
                                            "anyOf": [
                                                {
                                                    "description": "Information typically found at the bottom of the invoice.",
                                                    "type": "object",
                                                    "properties": {
                                                        "company_gstin_uin_at_footer": {
                                                            "description": "The company's GSTIN/UIN as stated in the footer, typically the supplier's.",
                                                            "type": "string"
                                                        },
                                                        "company_for_authorized_signatory": {
                                                            "description": "The name of the company for which the authorized signatory is signing, typically the supplier's.",
                                                            "type": "string"
                                                        }
                                                    },
                                                    "required": ["company_gstin_uin_at_footer", "company_for_authorized_signatory"],
                                                    "additionalProperties": False
                                                },
                                                {"type": "null"}
                                            ]
                                        }
                                    },
                                    "required": [
                                        "invoice_header", "customer_details", "supplier_details",
                                        "line_items", "summary", "footer_details"
                                    ],
                                    "additionalProperties": False
                                }
                            }
                        )

                        # Poll for completion
                        max_wait = 300
                        wait_interval = 10
                        elapsed = 0

                        while elapsed < max_wait:
                            job = client.extract.get(job.id)
                            status = (job.status or "").upper()
                            if status in ("SUCCESS", "COMPLETED"):
                                self.log(f"Extraction completed for {file['name']} (status={job.status})", "SUCCESS")
                                # ========== FIX: extract_result is where data lives ==========
                                extracted_data = (
                                    getattr(job, 'extract_result', None) or
                                    getattr(job, 'data', None) or
                                    getattr(job, 'result', None) or
                                    getattr(getattr(job, 'extraction', None), 'data', None) or
                                    getattr(getattr(job, 'output', None), 'data', None) or
                                    getattr(job, 'extracted_data', None) or
                                    getattr(job, 'json_result', None)
                                )
                                if extracted_data is None:
                                    self.log(f"WARNING: Could not find data in job. Full job: {vars(job)}", "WARNING")
                                break
                            elif status in ("FAILED", "ERROR", "CANCELLED"):
                                self.log(f"Extraction failed for {file['name']}: status={job.status}", "ERROR")
                                break
                            self.log(f"Extraction status: {job.status} ({elapsed}s elapsed)", "INFO")
                            time.sleep(wait_interval)
                            elapsed += wait_interval
                        else:
                            self.log(f"Extraction timed out after {max_wait}s for {file['name']}", "ERROR")

                    finally:
                        if os.path.exists(temp_path):
                            os.unlink(temp_path)

                    if not extracted_data or not isinstance(extracted_data, dict):
                        self.log(f"No valid extracted data for {file['name']}", "ERROR")
                        self.current_stats['pdf']['failed_pdfs'] += 1
                        continue

                    rows = self._process_extracted_data(extracted_data, file)
                    if rows:
                        self._save_to_sheets(config['spreadsheet_id'], sheet_name, rows)
                        processed_count += 1
                        self.current_stats['pdf']['processed_pdfs'] += 1
                        self.current_stats['pdf']['rows_added'] += len(rows)
                        total_rows += len(rows)
                        self.log(f"Successfully processed {file['name']}: {len(rows)} rows", "SUCCESS")
                    else:
                        self.log(f"No rows extracted from {file['name']}", "WARNING")

                    if progress_callback:
                        progress = 40 + (i + 1) / len(files_to_process) * 55
                        progress_callback(int(progress))

                except Exception as e:
                    self.log(f"Failed to process PDF {file['name']}: {str(e)}", "ERROR")
                    self.current_stats['pdf']['failed_pdfs'] += 1

            if progress_callback: progress_callback(100)
            if status_callback: status_callback(f"PDF workflow completed! Processed {processed_count} PDFs")
            self.log(f"PDF workflow completed! Processed {processed_count} PDFs, added {total_rows} rows", "SUCCESS")
            return {'success': True, 'processed': processed_count, 'rows_added': total_rows}

        except Exception as e:
            self.log(f"PDF workflow failed: {str(e)}", "ERROR")
            return {'success': False, 'processed': 0}

    def _list_drive_files(self, folder_id: str, days_back: int) -> List[Dict]:
        try:
            # Fix deprecation warning: use timezone-aware UTC
            start_datetime = datetime.now(timezone.utc) - timedelta(days=days_back - 1)
            start_str = start_datetime.strftime('%Y-%m-%dT00:00:00Z')
            query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false and createdTime >= '{start_str}'"
            all_files = []
            page_token = None
            while True:
                results = self.drive_service.files().list(
                    q=query, fields="nextPageToken, files(id, name, mimeType, createdTime, modifiedTime)",
                    orderBy="createdTime desc", pageSize=1000, pageToken=page_token
                ).execute()
                all_files.extend(results.get('files', []))
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
            return all_files
        except Exception as e:
            self.log(f"Failed to list files: {str(e)}", "ERROR")
            return []

    def _download_from_drive(self, file_id: str, file_name: str) -> bytes:
        try:
            return self.drive_service.files().get_media(fileId=file_id).execute()
        except Exception as e:
            self.log(f"Failed to download {file_name}: {str(e)}", "ERROR")
            return b""

    def _process_extracted_data(self, extracted_data: Dict, file_info: Dict) -> List[Dict]:
        rows = []
        # Handle both direct dict and nested "data" wrapper
        data = extracted_data.get("data", extracted_data)
        line_items = data.get("line_items")
        if not line_items:
            self.log(f"Skipping (no line_items): {file_info['name']}", "WARNING")
            return rows

        # Invoice header fields (nested object)
        invoice_header = data.get("invoice_header") or {}
        invoice_number = invoice_header.get("invoice_number", "")
        invoice_date = invoice_header.get("invoice_date", "")
        supplier_invoice_number = invoice_header.get("supplier_invoice_number") or ""
        supplier_invoice_date = invoice_header.get("supplier_invoice_date") or ""
        other_references = invoice_header.get("other_references") or ""

        # Customer details (nested object)
        customer = data.get("customer_details") or {}
        customer_name = customer.get("name", "")
        customer_address = customer.get("address", "")
        customer_gstin_uin = customer.get("gstin_uin", "")
        customer_state_name = customer.get("state_name") or ""
        customer_state_code = customer.get("state_code") or ""

        # Supplier details (nested object)
        supplier = data.get("supplier_details") or {}
        supplier_name = supplier.get("name", "")
        supplier_address = supplier.get("address", "")
        supplier_gstin_uin = supplier.get("gstin_uin", "")
        supplier_state_name = supplier.get("state_name") or ""
        supplier_state_code = supplier.get("state_code") or ""

        # Summary fields (nested object)
        summary = data.get("summary") or {}
        sub_total_amount = summary.get("sub_total_amount") if summary.get("sub_total_amount") is not None else 0
        total_quantity_of_items = summary.get("total_quantity_of_items") if summary.get("total_quantity_of_items") is not None else 0
        total_amount_payable = summary.get("total_amount_payable") if summary.get("total_amount_payable") is not None else 0
        amount_chargeable_in_words = summary.get("amount_chargeable_in_words", "")

        # Deductions (nested array inside summary) — concatenate into comma-separated strings.
        # Null/missing deductions safely fall back to empty strings.
        deductions = summary.get("deductions") or []
        if deductions:
            deduction_description = ", ".join(str(d.get("description", "") or "") for d in deductions)
            deduction_percentage = ", ".join(
                str(d.get("percentage")) if d.get("percentage") is not None else "" for d in deductions
            )
            deduction_amount = ", ".join(
                str(d.get("amount")) if d.get("amount") is not None else "" for d in deductions
            )
        else:
            deduction_description = ""
            deduction_percentage = ""
            deduction_amount = ""

        # Footer details (nested object, may be entirely null)
        footer = data.get("footer_details") or {}
        company_gstin_uin_at_footer = footer.get("company_gstin_uin_at_footer", "")
        company_for_authorized_signatory = footer.get("company_for_authorized_signatory", "")

        for item in line_items:
            item = item or {}
            row = {
                "invoice_number": invoice_number,
                "invoice_date": invoice_date,
                "supplier_invoice_number": supplier_invoice_number,
                "supplier_invoice_date": supplier_invoice_date,
                "other_references": other_references,
                "customer_name": customer_name,
                "customer_address": customer_address,
                "customer_gstin_uin": customer_gstin_uin,
                "customer_state_name": customer_state_name,
                "customer_state_code": customer_state_code,
                "supplier_name": supplier_name,
                "supplier_address": supplier_address,
                "supplier_gstin_uin": supplier_gstin_uin,
                "supplier_state_name": supplier_state_name,
                "supplier_state_code": supplier_state_code,
                "sl_no": item.get("sl_no") if item.get("sl_no") is not None else 0,
                "description_of_goods": item.get("description_of_goods") or "",
                "quantity": item.get("quantity") if item.get("quantity") is not None else 0,
                "unit": item.get("unit") or "",
                "rate": item.get("rate") if item.get("rate") is not None else 0,
                "amount": item.get("amount") if item.get("amount") is not None else 0,
                "sub_total_amount": sub_total_amount,
                "total_quantity_of_items": total_quantity_of_items,
                "total_amount_payable": total_amount_payable,
                "amount_chargeable_in_words": amount_chargeable_in_words,
                "deduction_description": deduction_description,
                "deduction_percentage": deduction_percentage,
                "deduction_amount": deduction_amount,
                "company_gstin_uin_at_footer": company_gstin_uin_at_footer,
                "company_for_authorized_signatory": company_for_authorized_signatory,
                "source_file": file_info['name'],
                "drive_file_id": file_info['id'],
                "processed_date": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            rows.append(row)
        return rows

    def _get_value(self, data, possible_keys, default=""):
        for key in possible_keys:
            if key in data:
                return data[key]
        return default

    def _save_to_sheets(self, spreadsheet_id: str, sheet_name: str, rows: List[Dict]):
        try:
            if not rows: return
            self._ensure_sheet_exists(spreadsheet_id, sheet_name)

            # STATIC_POS_HEADERS is the single source of truth — extracted data never changes this
            all_headers = STATIC_POS_HEADERS

            existing_headers = self._get_sheet_headers(spreadsheet_id, sheet_name)
            if existing_headers != all_headers:
                # Header is missing, empty, or has drifted — always restore the canonical header
                self._update_headers(spreadsheet_id, sheet_name, all_headers)
                if existing_headers:
                    self.log(
                        f"Header mismatch detected on sheet '{sheet_name}' — restored static headers. "
                        f"Was: {existing_headers[:5]}...",
                        "WARNING"
                    )
                else:
                    self.log(f"Sheet '{sheet_name}' was empty — wrote static headers.", "INFO")

            # Build data-only rows aligned to the fixed column order; missing keys become ""
            values = [[row.get(h, "") for h in all_headers] for row in rows]
            self._append_to_google_sheet(spreadsheet_id, sheet_name, values)
        except Exception as e:
            self.log(f"Failed to save to sheets: {str(e)}", "ERROR")

    def _get_sheet_headers(self, spreadsheet_id: str, sheet_name: str) -> List[str]:
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=f"{sheet_name}!A1:Z1", majorDimension="ROWS"
            ).execute()
            return result.get('values', [[]])[0]
        except:
            return []

    def _col_to_letter(self, n: int) -> str:
        """Convert 1-based column index to spreadsheet letters, e.g. 1→A, 26→Z, 27→AA."""
        result = ""
        while n > 0:
            n, remainder = divmod(n - 1, 26)
            result = chr(65 + remainder) + result
        return result

    def _update_headers(self, spreadsheet_id: str, sheet_name: str, headers: List[str]) -> bool:
        try:
            body = {'values': [headers]}
            last_col = self._col_to_letter(len(headers))
            self.sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_name}!A1:{last_col}1",
                valueInputOption='USER_ENTERED',
                body=body
            ).execute()
            self.log(f"Updated headers with {len(headers)} columns", "INFO")
            return True
        except Exception as e:
            self.log(f"Failed to update headers: {str(e)}", "ERROR")
            return False

    def _get_sheet_id(self, spreadsheet_id: str, sheet_name: str) -> int:
        try:
            metadata = self.sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
            for sheet in metadata.get('sheets', []):
                if sheet['properties']['title'] == sheet_name:
                    return sheet['properties']['sheetId']
            return 0
        except Exception as e:
            self.log(f"Failed to get sheet ID: {str(e)}", "ERROR")
            return 0

    def _get_sheet_data(self, spreadsheet_id: str, sheet_name: str) -> List[List[str]]:
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=sheet_name, majorDimension="ROWS"
            ).execute()
            return result.get('values', [])
        except Exception as e:
            self.log(f"Failed to get sheet data: {str(e)}", "ERROR")
            return []

    def _replace_rows_for_file(self, spreadsheet_id: str, sheet_name: str, file_name: str, headers: List[str], new_rows: List[List[Any]], sheet_id: int) -> bool:
        try:
            values = self._get_sheet_data(spreadsheet_id, sheet_name)
            if not values:
                return self._append_to_google_sheet(spreadsheet_id, sheet_name, new_rows)
            current_headers = values[0]
            try:
                file_col = current_headers.index('source_file')
            except ValueError:
                return self._append_to_google_sheet(spreadsheet_id, sheet_name, new_rows)

            data_rows = values[1:]  # rows after the header
            rows_to_delete = [idx for idx, row in enumerate(data_rows, 2) if len(row) > file_col and row[file_col] == file_name]
            if rows_to_delete:
                rows_to_delete.sort(reverse=True)
                requests = [{'deleteDimension': {'range': {'sheetId': sheet_id, 'dimension': 'ROWS', 'startIndex': r-1, 'endIndex': r}}} for r in rows_to_delete]
                self.sheets_service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={'requests': requests}).execute()
                self.log(f"Deleted {len(rows_to_delete)} existing rows for {file_name}", "INFO")

            return self._append_to_google_sheet(spreadsheet_id, sheet_name, new_rows)
        except Exception as e:
            self.log(f"Failed to replace rows: {str(e)}", "ERROR")
            return False

    def _append_to_google_sheet(self, spreadsheet_id: str, range_name: str, values: List[List[Any]]) -> bool:
        for attempt in range(1, 4):
            try:
                body = {'values': values}
                self.sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id, range=range_name, valueInputOption='USER_ENTERED', body=body
                ).execute()
                return True
            except Exception as e:
                if attempt < 3:
                    time.sleep(2)
                else:
                    self.log(f"Failed to append after 3 attempts: {str(e)}", "ERROR")
                    return False
        return False

    def _ensure_sheet_exists(self, spreadsheet_id: str, sheet_name: str):
        try:
            metadata = self.sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
            existing = [s['properties']['title'] for s in metadata.get('sheets', [])]
            if sheet_name not in existing:
                body = {'requests': [{'addSheet': {'properties': {'title': sheet_name}}}]}
                self.sheets_service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()
                self.log(f"Created sheet tab: {sheet_name}", "INFO")
        except Exception as e:
            self.log(f"Failed to ensure sheet exists: {str(e)}", "WARNING")

    def _log_workflow_to_sheet(self, spreadsheet_id: str, sheet_range: str, start_time: datetime, end_time: datetime, stats: Dict):
        try:
            sheet_name = sheet_range.split('!')[0] if '!' in sheet_range else sheet_range
            self._ensure_sheet_exists(spreadsheet_id, sheet_name)
            headers = [
                "Start Time", "End Time",
                "Gmail Processed Emails", "Gmail Total Attachments", "Gmail Successful Uploads", "Gmail Failed Uploads",
                "PDF Total PDFs", "PDF Processed PDFs", "PDF Skipped PDFs", "PDF Failed PDFs", "PDF Rows Added"
            ]
            log_row = [
                start_time.strftime("%Y-%m-%d %H:%M:%S"),
                end_time.strftime("%Y-%m-%d %H:%M:%S"),
                stats['gmail']['processed_emails'],
                stats['gmail']['total_attachments'],
                stats['gmail']['successful_uploads'],
                stats['gmail']['failed_uploads'],
                stats['pdf']['total_pdfs'],
                stats['pdf']['processed_pdfs'],
                stats['pdf']['skipped_pdfs'],
                stats['pdf']['failed_pdfs'],
                stats['pdf']['rows_added']
            ]
            values = self._get_sheet_data(spreadsheet_id, sheet_name)
            if not values or values[0] != headers:
                self._append_to_google_sheet(spreadsheet_id, sheet_range, [headers])
            self._append_to_google_sheet(spreadsheet_id, sheet_range, [log_row])
            self.log("Workflow logged to sheet", "SUCCESS")
        except Exception as e:
            self.log(f"Failed to log workflow: {str(e)}", "ERROR")


def run_combined_workflow(automation):
    start_time = datetime.now()
    automation.log("Starting combined workflow...")
    automation.reset_stats()
    
    gmail_config = CONFIG['gmail']
    pdf_config = CONFIG['pdf']
    
    def update_progress(value): automation.log(f"Progress: {value}%")
    def update_status(message): automation.log(message)
    
    gmail_result = automation.process_gmail_workflow(gmail_config, progress_callback=update_progress, status_callback=update_status)
    
    if not gmail_result.get('success'):
        automation.log("Gmail workflow failed", "ERROR")
        end_time = datetime.now()
        automation._log_workflow_to_sheet(pdf_config['spreadsheet_id'], CONFIG['log']['sheet_range'], start_time, end_time, automation.current_stats)
        return {"status": "failed"}
    
    pdf_result = automation.process_pdf_workflow(pdf_config, progress_callback=update_progress, status_callback=update_status)
    
    end_time = datetime.now()
    automation.log(f"Combined workflow completed! Gmail: {gmail_result.get('processed',0)} attachments | PDF: {pdf_result.get('processed',0)} PDFs, {pdf_result.get('rows_added',0)} rows", "SUCCESS")
    automation._log_workflow_to_sheet(pdf_config['spreadsheet_id'], CONFIG['log']['sheet_range'], start_time, end_time, automation.current_stats)
    
    return {
        "status": "success",
        "gmail_attachments": gmail_result.get('processed', 0),
        "pdf_pdfs": pdf_result.get('processed', 0),
        "pdf_skipped": automation.current_stats['pdf']['skipped_pdfs'],
        "pdf_rows": pdf_result.get('rows_added', 0)
    }


def main():
    automation = MilkbasketAutomation()
    if not automation.authenticate():
        logger.error("Authentication failed. Exiting.")
        return
    
    result = run_combined_workflow(automation)
    logger.info(f"Workflow result: {result}")
    
    schedule.every(3).hours.do(run_combined_workflow, automation)
    logger.info("Scheduler started. Running every 3 hours.")
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")

if __name__ == "__main__":
    main()