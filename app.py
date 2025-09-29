from flask import Flask, render_template, request, redirect, url_for
import os
import subprocess
import re
import json

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# regex para limpar sequências ANSI/terminal (spinners)
ANSI_RE = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')

# controla se vamos usar o Ollama ou apenas fallback
USE_OLLAMA = os.getenv("USE_OLLAMA", "true").lower() == "true"

# classificador por palavra-chave (fallback rápido e determinístico)
def classificador_por_palavra_chave(texto):
    texto_l = texto.lower()
    palavras_prod = ["urgente", "erro", "relatório", "importante", "atualização", "servidor", "falha", "incidente", "ajuda", "ticket", "suporte"]
    for p in palavras_prod:
        if p in texto_l:
            return "Mina"
    return "Improdutivo"

def chamar_ollama_try_variants(prompt, timeout=60):
    if not USE_OLLAMA:
        return "", "ollama_disabled", 0, "disabled"

    attempts = [
        (["ollama", "run", "llama2", "--stdin"], True, "stdin_with_flag"),
        (["ollama", "run", "llama2"], True, "stdin_no_flag"),
        (["ollama", "run", "llama2", prompt], False, "prompt_arg"),
    ]

    for cmd, use_stdin, label in attempts:
        try:
            if use_stdin:
                proc = subprocess.run(cmd, input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            else:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)

            out = proc.stdout.decode("utf-8", errors="ignore") if isinstance(proc.stdout, bytes) else proc.stdout
            err = proc.stderr.decode("utf-8", errors="ignore") if isinstance(proc.stderr, bytes) else proc.stderr

            if err and ("unknown flag" in err.lower() or "unrecognized option" in err.lower()):
                continue

            return out or "", err or "", proc.returncode, label

        except subprocess.TimeoutExpired:
            return "", "timeout", 124, label
        except FileNotFoundError:
            return "", "ollama_not_found", 127, label
        except Exception as e:
            return "", f"exception: {e}", 1, label

    return "", "no_method_worked", 1, "none"

def parsear_output_model(raw):
    clean = ANSI_RE.sub("", raw).strip()
    if not clean:
        return None, None, raw

    try:
        data = json.loads(clean)
        if isinstance(data, dict):
            classification = data.get("classification") or data.get("classificacao")
            response = data.get("response") or data.get("resposta")
            if classification:
                classification = classification.strip()
            return classification or None, response or None, clean
    except Exception:
        pass

    classificacao = None
    resposta = []
    for line in clean.splitlines():
        l = line.strip()
        if not l:
            continue
        low = l.lower()
        if "classifica" in low or low.startswith("classification"):
            if "mina" in low:
                classificacao = "Mina"
            elif "improdutivo" in low:
                classificacao = "Improdutivo"
            else:
                parts = l.split(":", 1)
                if len(parts) == 2:
                    val = parts[1].strip()
                    if "mina" in val.lower():
                        classificacao = "Mina"
                    elif "improdutivo" in val.lower():
                        classificacao = "Improdutivo"
        elif low.startswith("resposta") or low.startswith("response"):
            parts = l.split(":", 1)
            if len(parts) == 2:
                resposta.append(parts[1].strip())
        else:
            if classificacao:
                resposta.append(l)

    if not classificacao:
        if "mina" in clean.lower():
            classificacao = "Mina"
        elif "improdutivo" in clean.lower():
            classificacao = "Improdutivo"

    resposta_texto = " ".join(resposta).strip()
    if not resposta_texto:
        sents = re.split(r'(?<=[\.\?\!])\s+', clean)
        sents = [s for s in sents if 'classific' not in s.lower() and s.strip()]
        resposta_texto = " ".join(sents[:2]).strip() if sents else None

    return classificacao, resposta_texto, clean

def processar_mensagem_com_modelo(mensagem):
    prompt = f"""
Você é um assistente de e-mails que responde sempre em português do Brasil.

Leia a mensagem entre as marcas <<< >>> abaixo e, em seguida, RETORNE APENAS um JSON válido com duas chaves:
- "classification": deve ser exatamente "Mina" OU "Improdutivo"
- "response": uma sugestão de resposta curta (1-3 frases), em português do Brasil.

Não escreva nada além do JSON. Exemplo de saída:
{{"classification": "Mina", "response": "Obrigado, vamos analisar e retornamos até hoje."}}

Mensagem:
<<<
{mensagem}
>>>
"""
    raw_out, raw_err, code, method = chamar_ollama_try_variants(prompt, timeout=90)
    print("=== DEBUG: Ollama method:", method, "returncode:", code)
    print("=== DEBUG: raw_err:", raw_err)
    print("=== DEBUG: raw_out:", raw_out[:500])

    classification, response_sugerida, cleaned = parsear_output_model(raw_out)

    debug = {
        "method": method,
        "returncode": code,
        "raw_err": raw_err,
        "raw_out_sample": raw_out[:500],
        "cleaned": cleaned
    }

    if (not classification) or (classification not in ("Mina", "Improdutivo")):
        classification = classificador_por_palavra_chave(mensagem)
        if classification == "Mina":
            response_sugerida = "Recebemos seu e-mail e iremos verificar o assunto. Retornaremos assim que possível."
        else:
            response_sugerida = "Obrigado pela mensagem! Caso precise de algo relacionado ao suporte, nos avise."
        debug["fallback_used"] = True
    else:
        debug["fallback_used"] = False

    return classification, response_sugerida, debug

@app.route('/', methods=['GET', 'POST'])
def index():
    resultado = None
    if request.method == 'POST':
        if 'arquivo' not in request.files:
            return render_template('index.html', resultado="Nenhum arquivo selecionado")
        
        arquivo = request.files['arquivo']
        if arquivo.filename == '':
            return render_template('index.html', resultado="Nenhum arquivo selecionado")
        
        caminho_arquivo = os.path.join(app.config['UPLOAD_FOLDER'], arquivo.filename)
        arquivo.save(caminho_arquivo)
        
        try:
            with open(caminho_arquivo, 'r', encoding='utf-8') as f:
                mensagem = f.read()
        except Exception:
            with open(caminho_arquivo, 'r', encoding='latin-1') as f:
                mensagem = f.read()

        classificacao, resposta_sugerida, debug = processar_mensagem_com_modelo(mensagem)

        resultado = {
            "classificacao": classificacao,
            "mensagem": mensagem,
            "resposta": resposta_sugerida,
            "debug": debug
        }

    return render_template('index.html', resultado=resultado)

@app.route('/enviar', methods=['POST'])
def enviar():
    resposta = request.form.get("resposta")
    print("📧 [SIMULADO] Resposta enviada:", resposta)
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)