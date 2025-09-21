import os
import re
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, send_file, flash
from werkzeug.utils import secure_filename
import pandas as pd
import plotly.express as px


# ---------- CONFIG ----------
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.secret_key = 'change-this-to-a-random-secret'  # for flash messages


# ---------- Predefined Normal Ranges & Recommendations ----------
# These are simple general ranges — you can expand or customize them.
NORMAL_RANGES = {
    'Hemoglobin': (12.0, 16.0, 'g/dL'),
    'Vitamin D': (20.0, 50.0, 'ng/mL'),
    'Cholesterol': (0.0, 200.0, 'mg/dL'),
    'Glucose': (70.0, 140.0, 'mg/dL'),  # casual range — adjust to fasting vs random as needed
    'HDL': (40.0, 60.0, 'mg/dL'),
    'LDL': (0.0, 100.0, 'mg/dL'),
    'Triglycerides': (0.0, 150.0, 'mg/dL'),
    'WBC': (4.0, 11.0, 'x10^3/µL'),
    'RBC': (4.5, 5.9, 'x10^6/µL'),
    'Platelets': (150.0, 450.0, 'x10^3/µL'),
    'Blood Pressure': (None, None, 'mmHg'),  # Special handling
    # Add more as needed
}


RECOMMENDATIONS = {
    'Hemoglobin_low': 'Consider iron-rich foods (spinach, red meat, lentils). Consult physician for anemia evaluation.',
    'Hemoglobin_high': "Could indicate dehydration or other issues; consult a doctor.",
    'Vitamin D_low': 'Increase sun exposure and consider Vitamin D supplementation after consulting a physician.',
    'Vitamin D_high': "Excess supplements; consult doctor.",
    'Cholesterol_high': 'Reduce saturated fats, avoid processed foods, increase fiber and exercise. Consider lipid profile review with a doctor.',
    'Cholesterol_low': "May indicate malnutrition or other issues.",
    'Glucose_high': 'Reduce sugar and refined carbs, increase physical activity, monitor blood glucose and consult doctor for diabetes evaluation.',
    'Glucose_low': "Could cause hypoglycemia; eat balanced diet.",
    'Blood Pressure_high': "Risk of hypertension; reduce salt, exercise.",
    'Blood Pressure_low': "May cause dizziness; consult doctor.",
    'default_low': 'Value below normal — consider medical follow-up.',
    'default_high': 'Value above normal — consider medical follow-up.'
}


# Disease risk combinations

DISEASE_RULES = [
    {
        "conditions": [
            ("Hemoglobin", "High"),
            ("Cholesterol", "Low")
        ],
        "disease": "Possible Polycythemia or metabolic disorder",
        "advice": "Consult a hematologist; unusual profile, detailed tests needed."
    },
    {
        "conditions": [
            ("Cholesterol", "High"),
            ("Glucose", "High"),
            ("Blood Pressure", "High")
        ],
        "disease": "Metabolic Syndrome",
        "advice": "Risk of diabetes/heart disease; lifestyle changes strongly advised."
    },
    {
        "conditions": [
            ("Vitamin D", "Low"),
            ("Calcium", "Low")
        ],
        "disease": "Osteoporosis Risk",
        "advice": "Bone weakness possible; increase Vitamin D and Calcium intake."
    }
]

# ---------- Utilities ----------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# Parsing helper - extract test name, numeric value (or BP), unit (if present)

def normalize_test_name(name):
    # Basic normalization mapping for common synonyms
    name = name.lower().strip()
    mapping = {
        'hb': 'Hemoglobin',
        'hemoglobin': 'Hemoglobin',
        'vit d': 'Vitamin D',
        'vitamin d': 'Vitamin D',
        'cholesterol total': 'Cholesterol',
        'cholesterol': 'Cholesterol',
        'glucose': 'Glucose',
        'blood glucose': 'Glucose',
        'sugar': 'Glucose',
        'hdl': 'HDL',
        'ldl': 'LDL',
        'triglycerides': 'Triglycerides',
        'wbc': 'WBC',
        'rbc': 'RBC',
        'platelets': 'Platelets',
        'bp': 'Blood Pressure',
        'blood pressure': 'Blood Pressure'
    }
    # try exact mapping
    if name in mapping:
        return mapping[name]
    # try keywords
    for k,v in mapping.items():
        if k in name:
            return v
    # fallback title case
    return name.title()


def evaluate_record(rec):
    test = normalize_test_name(rec['Test'])
    value = rec['Value']
    unit = rec.get('Unit', '')
    status = 'Unknown'
    suggestion = None


    # Blood pressure special case (value like '120/80')
    if isinstance(value, str) and '/' in value:
        try:
            s, d = value.split('/')
            s = int(s); d = int(d)
            # use simple thresholds:
            if s < 90 or d < 60:
                status = 'Low'
            elif s <= 120 and d <= 80:
                status = 'Normal'
            elif s <= 139 or d <= 89:
                status = 'Elevated'
            else:
                status = 'High'
            suggestion = RECOMMENDATIONS.get('default_high' if status in ('Elevated', 'High') else 'default_low')
        except:
            status = 'Unknown'
    else:
        # numeric case
        try:
            val = float(value)
            if test in NORMAL_RANGES:
                mn, mx, _u = NORMAL_RANGES[test]
                if val < mn:
                    status = 'Low'
                    suggestion = RECOMMENDATIONS.get(f'{test}_low', RECOMMENDATIONS.get('default_low'))
                elif val > mx:
                    status = 'High'
                    suggestion = RECOMMENDATIONS.get(f'{test}_high', RECOMMENDATIONS.get('default_high'))
                else:
                    status = 'Normal'
                    suggestion = 'Within normal range.'
            else:
                # unknown test -> no range
                status = 'Unknown'
                suggestion = 'No reference range available for this test.'
        except:
            status = 'Unknown'


    return {
        'Test': test,
        'Value': value,
        'Unit': unit,
        'Status': status,
        'Recommendation': suggestion
    }


def check_disease_combinations(results_df):
    detected = []
    for rule in DISEASE_RULES:
        match = True
        for (test, status) in rule["conditions"]:
            found = results_df[(results_df["Test"] == test) & (results_df["Status"] == status)]
            if found.empty:
                match = False
                break
        if match:
            detected.append({
                "Disease": rule["disease"],
                "Advice": rule["advice"]
            })
    return detected


# ---------- Routes ----------
@app.route('/', methods=['GET', 'POST'])
def index():
    results = None
    chart_html = None
    filename = None
    disease_risks = []
    
    if request.method == 'POST':
        # file uploaded
        if 'report' not in request.files:
            flash('No file part')
            return redirect(request.url)
        
        file = request.files['report']
        if file.filename == '':
            flash('No selected file')
            return redirect(request.url)
        
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            saved_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}")
            file.save(saved_path)

            try:
                df = pd.read_csv(saved_path)
                 # Expected columns: Test, Value, Unit, Normal_Min, Normal_Max
                if 'Test' not in df.columns or 'Value' not in df.columns:
                    flash('CSV must contain at least "Test" and "Value" columns.')
                    return redirect(request.url)

                records = []
                for _, row in df.iterrows():
                    records.append({
                        'Test': str(row['Test']),
                        'Value': row['Value'],
                        'Unit': row.get('Unit', '')
                    })
            except Exception as e:
                flash('Failed to read CSV: ' + str(e))
                return redirect(request.url)

            # Normalize and evaluate
            evaluated = [evaluate_record(r) for r in records]
            results = pd.DataFrame(evaluated)

            # Disease risk check
            disease_risks = check_disease_combinations(results)
            
            # small plotting: numeric values only (skip BP non-numeric)
            numeric_df = results[results['Status'] != 'Unknown'].copy()
            # convert Value if numeric
            def value_for_plot(v):
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, str) and '/' not in v:
                    try:
                        return float(v)
                    except:
                        return None
                return None
            numeric_df['PlotValue'] = numeric_df['Value'].apply(value_for_plot)
            plot_df = numeric_df.dropna(subset=['PlotValue'])
            if not plot_df.empty:
                color_map = {'Low':'orange', 'Normal':'green', 'High':'red', 'Elevated':'orange'}
                fig = px.bar(plot_df, x='Test', y='PlotValue', color='Status',
                             color_discrete_map=color_map, title='Biomarker Values')
                # add normal range lines (if test present)
                # simple: no shapes; judges only need the colored bars
                chart_html = fig.to_html(full_html=False)


            # Save processed CSV
            out_csv = saved_path + '_results.csv'
            results.to_csv(out_csv, index=False)
            request_results_path = out_csv

            return render_template('index.html', results=results.to_dict(orient='records'), chart=chart_html, filename=os.path.basename(saved_path), disease_risks=disease_risks)
        

    return render_template('index.html', results=None, chart=None, filename=None, disease_risks=[])


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
