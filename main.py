import os
import io
import re
import json
import csv
import tempfile
from flask import Flask, request, jsonify, send_from_directory, render_template_string
from werkzeug.utils import secure_filename
import pymysql
from dotenv import load_dotenv

try:
    import fitz
except:
    fitz = None

try:
    import google.generativeai as genai
except:
    genai = None
load_dotenv()

DB_HOST = os.getenv('DB_HOST') DB_USER = os.getenv('DB_USER') DB_PASS = os.getenv('DB_PASS') DB_NAME = os.getenv('DB_NAME') DB_PORT = int(os.getenv('DB_PORT', '3306')) GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads') GENERATED_FOLDER = os.path.join(os.getcwd(), 'generated') os.makedirs(UPLOAD_FOLDER, exist_ok=True) os.makedirs(GENERATED_FOLDER, exist_ok=True) ALLOWED_EXT = {'pdf'}

if GEMINI_API_KEY and genai: genai.configure(api_key=GEMINI_API_KEY) GEMINI_AVAILABLE = True else: GEMINI_AVAILABLE = False

app = Flask(name) app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER app.config['GENERATED_FOLDER'] = GENERATED_FOLDER app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

def allowed_file(filename): return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

def extract_text_from_pdf(pdf_path: str): if fitz is None: raise RuntimeError('PyMuPDF (fitz) not installed') doc = fitz.open(pdf_path) pages = [doc.load_page(i).get_text().strip() for i in range(len(doc))] return pages

def chunk_passages(pages_text, max_chars=2800): passages = [] buffer = '' for p in pages_text: if not p.strip(): continue parts = re.split(r'\n\s*\n', p) for part in parts: part = part.strip() if len(buffer)+len(part)+2 > max_chars: if buffer: passages.append(buffer.strip()) buffer = part else: buffer = (buffer + '\n\n' + part) if buffer else part if buffer: passages.append(buffer.strip()) return passages

LLM_PROMPT = ''' তুমি একজন অভিজ্ঞ প্রশ্ন প্রস্তুতকারী, Bangladesh admission test (Medical/Engineering/Varsity) স্টাইলে প্রশ্ন তৈরি করো। Generate up to {max_q} MCQs from the passage below. Prefer Bengali if passage is Bengali. Use only facts present in passage. Respond with a JSON array only. Each item: {{ "question": "...", "options": {{"A":"...","B":"...","C":"...","D":"..."}}, "correct_option": "A|B|C|D|A,C|B,D", "explanation": "short explanation (Bangla or English)", "subject": "Biology|Chemistry|Physics|Math|English|General", "chapter": "...", "difficulty": "Easy|Medium|Hard", "year": "" }} Passage:

{passage}

Exam style: {exam_type} Subject hint: {subject_hint} '''

def generate_with_gemini(prompt_text, model_name='gemini-1.5'): if not GEMINI_AVAILABLE: raise RuntimeError('Gemini not configured') resp = genai.generate_text(model=model_name, prompt=prompt_text) return resp['candidates'][0]['content']

def generate_mcqs_from_passage(passage, max_q=4, exam_type='General', subject_hint=''): prompt = LLM_PROMPT.format(max_q=max_q, passage=passage[:15000], exam_type=exam_type, subject_hint=subject_hint) text = generate_with_gemini(prompt) start = text.find('[') end = text.rfind(']') json_text = text[start:end+1] if start!=-1 and end!=-1 else text data = json.loads(json_text) out = [] for item in data: opts = item.get('options', {}) for k in ['A','B','C','D']: opts.setdefault(k,'') out.append({ 'question': item.get('question','').strip(), 'options': {'A': opts['A'].strip(), 'B': opts['B'].strip(), 'C': opts['C'].strip(), 'D': opts['D'].strip()}, 'correct_option': item.get('correct_option','').strip(), 'explanation': item.get('explanation','').strip(), 'subject': item.get('subject','').strip() or subject_hint, 'chapter': item.get('chapter','').strip(), 'difficulty': item.get('difficulty','').strip() or 'Medium', 'year': item.get('year','').strip() or '' }) return out

def save_csv(questions, path): headers = ['subject','chapter','question','option_a','option_b','option_c','option_d','correct_option','explanation','difficulty','year'] with open(path, 'w', newline='', encoding='utf-8') as f: w = csv.writer(f) w.writerow(headers) for q in questions: w.writerow([q['subject'], q['chapter'], q['question'], q['options']['A'], q['options']['B'], q['options']['C'], q['options']['D'], q['correct_option'], q['explanation'], q['difficulty'], q['year']])

def insert_into_db(questions): conn = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME, port=DB_PORT, charset='utf8mb4') try: with conn.cursor() as cur: def get_or_create_subject(name): cur.execute('SELECT id FROM subjects WHERE name=%s', (name,)) r = cur.fetchone() if r: return r[0] cur.execute('INSERT INTO subjects (name) VALUES (%s)', (name,)) return cur.lastrowid def get_or_create_chapter(name, sid): if not name or not sid: return None cur.execute('SELECT id FROM chapters WHERE name=%s AND subject_id=%s', (name, sid)) r = cur.fetchone() if r: return r[0] cur.execute('INSERT INTO chapters (subject_id,name) VALUES (%s,%s)', (sid,name)) return cur.lastrowid for q in questions: sid = get_or_create_subject(q['subject']) cid = get_or_create_chapter(q['chapter'], sid) cur.execute('''INSERT INTO questions (subject_id, chapter_id, question_text, option_a, option_b, option_c, option_d, correct_option, explanation, difficulty, year) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''', (sid, cid, q['question'], q['options']['A'], q['options']['B'], q['options']['C'], q['options']['D'], q['correct_option'], q['explanation'], q['difficulty'], q['year'])) conn.commit() finally: conn.close()

UPLOAD_HTML = '''<html><head><meta charset='utf-8'><title>ASTAP Prep</title></head><body><h2>Upload PDF (Bangla/English)</h2><form action='/upload' method='post' enctype='multipart/form-data'><input type='file' name='file' required><br>Exam Type:<select name='exam_type'><option>General</option><option>Medical</option><option>Engineering</option><option>Varsity</option></select><br>Max Q per passage:<input type='number' name='max_per' value='4'><br>Subject hint (optional):<input name='subject_hint'><br>Insert to DB:<input type='checkbox' name='insert_db' value='1'><br><button type='submit'>Upload & Generate</button></form></body></html>''' PREVIEW_HTML = '''<html><head><meta charset='utf-8'><title>Preview</title></head><body><h2>Preview {{count}} questions</h2><a href='/download/{{csv_name}}'>Download CSV</a><br><a href='/'>Upload another</a>{% for q in questions %}<div style='border:1px solid #ccc;padding:10px;margin:5px;'><b>Subject:</b>{{q.subject}} <b>Chapter:</b>{{q.chapter}}<br><b>Q:</b>{{q.question}}<br>A: {{q.options.A}}<br>B: {{q.options.B}}<br>C: {{q.options.C}}<br>D: {{q.options.D}}<br><b>Answer:</b> {{q.correct_option}} <b>Explanation:</b>{{q.explanation}}</div>{% endfor %}</body></html>'''

@app.route('/', methods=['GET']) def index(): return UPLOAD_HTML

@app.route('/upload', methods=['POST']) def upload(): file = request.files.get('file') if not file or not allowed_file(file.filename): return 'Invalid file', 400 filename = secure_filename(file.filename) path = os.path.join(app.config['UPLOAD_FOLDER'], filename) file.save(path)

exam_type = request.form.get('exam_type','General')
max_per = int(request.form.get('max_per',4))
subject_hint = request.form.get('subject_hint','')
insert_db = request.form.get('insert_db')=='1'

pages_text = extract_text_from_pdf(path)
passages = chunk_passages(pages_text)

all_questions = []
for p in passages:
    try:
        qs = generate_mcqs_from_passage(p,max_q=max_per,exam_type=exam_type,subject_hint=subject_hint)
        all_questions.extend(qs)
    except Exception as e:
        print('Gemini API Error:', e)

csv_name = filename.rsplit('.',1)[0]+'_mcqs.csv'
csv_path = os.path.join(app.config['GENERATED_FOLDER'], csv_name)
save_csv(all_questions, csv_path)

if insert_db:
    try:
        insert_into_db(all_questions)
    except Exception as e:
        print('DB Insert Error:', e)

return render_template_string(PREVIEW_HTML, questions=all_questions, count=len(all_questions), csv_name=csv_name)

@app.route('/download/<csv_name>') def download(csv_name): return send_from_directory(app.config['GENERATED_FOLDER'], csv_name, as_attachment=True)

if name=='main': app.run(host='0.0.0.0', port=int(os.environ.get('PORT',5000)))

