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

# classificador por palavra-chave (fallback rápido e determinístico)
def classificador_por_palavra_chave(texto):
    texto_l = texto.lower()
    palavras_prod = ["urgente", "erro", "relatório", "importante", "atualização", "servidor", "falha", "incidente", "ajuda", "ticket", "suporte"]
    for p in palavras_prod:
        if p in texto_l:
            return "Mina"
    return "Improdutivo"

def chamar_ollama_try_variants(prompt, timeout=60):
    """
    Tenta chamar o Ollama de 3 formas possíveis:
      1) ['ollama','run','llama2','--stdin']  (com prompt por stdin)
      2) ['ollama','run','llama2'] (com prompt por stdin)
      3) ['ollama','run','llama2', prompt] (prompt como argumento)
    Retorna (stdout, stderr, returncode, method_used)
    """
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
                # quando passamos o prompt como argumento, evite passar bytes no input
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)

            out = proc.stdout.decode("utf-8", errors="ignore") if isinstance(proc.stdout, bytes) else proc.stdout
            err = proc.stderr.decode("utf-8", errors="ignore") if isinstance(proc.stderr, bytes) else proc.stderr

            # se stderr indicar flag desconhecida, pule para próxima tentativa
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
    """
    Recebe raw string do modelo (já sem ANSI) e tenta extrair:
      - classification: "Mina" / "Improdutivo" / "Indefinido"
      - response: texto da sugestão
    Estratégia:
      1) tenta json.loads
      2) procura por linhas que comecem com 'Classificação' / 'Classificacao' / 'Classification'
      3) procura por 'Resposta' / 'Response'
      4) fallback heurístico
    """
    clean = ANSI_RE.sub("", raw).strip()
    if not clean:
        return None, None, raw

    # 1) tenta JSON primeiro
    try:
        data = json.loads(clean)
        if isinstance(data, dict):
            classification = data.get("classification") or data.get("classificacao") or data.get("classification".lower())
            response = data.get("response") or data.get("resposta")
            if classification:
                classification = classification.strip()
            return classification or None, response or None, clean
    except Exception:
        pass

    # 2) parse por linhas
    classificacao = None
    resposta = []
    for line in clean.splitlines():
        l = line.strip()
        if not l:
            continue
        low = l.lower()
        # detecção de linha de classificação
        if "classifica" in low or low.startswith("classification") or low.startswith("classificacao") or low.startswith("classificação"):
            # extrai a palavra 'Mina' ou 'Improdutivo' se presente
            if "mina" in low:
                classificacao = "Mina"
            elif "improdutivo" in low:
                classificacao = "Improdutivo"
            else:
                # tenta depois do ':' se houver
                parts = l.split(":", 1)
                if len(parts) == 2:
                    val = parts[1].strip()
                    if "mina" in val.lower():
                        classificacao = "Mina"
                    elif "improdutivo" in val.lower():
                        classificacao = "Improdutivo"
        # detecção de resposta
        elif low.startswith("resposta") or low.startswith("response") or low.startswith("reply"):
            parts = l.split(":", 1)
            if len(parts) == 2:
                resposta.append(parts[1].strip())
        else:
            # se já encontramos classificação, linhas seguintes podem ser a resposta
            if classificacao:
                resposta.append(l)

    # se não achou classificacao explicitamente, busca token na saída inteira
    if not classificacao:
        if "mina" in clean.lower():
            classificacao = "Mina"
        elif "improdutivo" in clean.lower():
            classificacao = "Improdutivo"

    # montar resposta final
    resposta_texto = " ".join(resposta).strip()
    if not resposta_texto:
        # fallback: pegar as primeiras 2 frases que não mencionem 'classifica'
        sents = re.split(r'(?<=[\.\?\!])\s+', clean)
        sents = [s for s in sents if 'classific' not in s.lower() and s.strip()]
        resposta_texto = " ".join(sents[:2]).strip() if sents else None

    return classificacao, resposta_texto, clean

def processar_mensagem_com_modelo(mensagem):
    """
    Chama o modelo, processa a saída e retorna (classificacao, resposta_sugerida, debug_info)
    """
    # Prompt bem explícito pedindo JSON — tenta forçar saída estruturada
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
    print("=== DEBUG: raw_out (first 1000 chars):", (raw_out[:1000] + '...') if raw_out and len(raw_out) > 1000 else raw_out)

    # limpar e parsear
    classification, response_sugerida, cleaned = parsear_output_model(raw_out)

    debug = {
        "method": method,
        "returncode": code,
        "raw_err": raw_err,
        "raw_out_sample": (raw_out[:2000] + '...') if raw_out and len(raw_out)>2000 else raw_out,
        "cleaned": cleaned
    }

    # fallback: se modelo não respondeu corretamente, usa classificador de palavra-chave
    if (not classification) or (classification not in ("Mina", "Improdutivo")):
        # use fallback rules
        classification = classificador_por_palavra_chave(mensagem)
        # resposta padrão curta dependendo da classificação
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
        
        # Lê o arquivo com UTF-8 com fallback
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
            "debug": debug  # você pode remover isso quando estiver confiante
        }

    return render_template('index.html', resultado=resultado)

@app.route('/enviar', methods=['POST'])
def enviar():
    resposta = request.form.get("resposta")
    # Implementação de envio real pode ser adicionada aqui (SMTP / API)
    print("📧 [SIMULADO] Resposta enviada:", resposta)
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)