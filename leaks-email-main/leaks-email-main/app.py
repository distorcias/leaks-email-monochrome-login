from flask import Flask, render_template, request, redirect, url_for, session, abort
import requests
import secrets
import os
import random
import psycopg2

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# Senhas separadas para cada Administrador
ADMIN_1_PASSWORD = "administrador_owner"
ADMIN_2_PASSWORD = "administrador_socio"

DUCKMAIL_API = "https://api.duckmail.sbs"

# Configurações
DB_HOST = "aws-0-sa-east-1.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = "postgres.dsfsbmklnkpluzohuvoe"
DB_PASSWORD = "GgRL2UEQXzPYbBwa"
DB_PORT = "6543"

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
        sslmode="require"
    )

def init_db():
    """Garante que a tabela existe no Supabase e tem a coluna owner"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS mailboxes (
                email TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                token TEXT NOT NULL,
                owner TEXT DEFAULT 'admin1'
            )
        ''')
        cursor.execute('''
            ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS owner TEXT DEFAULT 'admin1';
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print("Erro ao inicializar DB no Supabase:", e)

init_db()

def get_mailboxes_by_owner(owner):
    """Busca apenas os e-mails salvos pelo administrador logado específico"""
    mailboxes = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT email, password, token FROM mailboxes WHERE owner = %s", (owner,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        for row in rows:
            mailboxes[row[0]] = {
                "password": row[1],
                "token": row[2]
            }
    except Exception as e:
        print("Erro ao ler do Supabase:", e)
    return mailboxes

def get_all_mailboxes_global():
    """Busca todos os e-mails globais (para validação de login dos clientes)"""
    mailboxes = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT email, password, token FROM mailboxes")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        for row in rows:
            mailboxes[row[0]] = {
                "password": row[1],
                "token": row[2]
            }
    except Exception as e:
        print("Erro ao ler do Supabase:", e)
    return mailboxes

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/client-login", methods=["POST"])
@app.route("/open-mailbox", methods=["POST"])
def client_login():
    """Autentica o utilizador com as credenciais da conta DuckMail já registada."""
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    if not email or not password:
        return redirect(url_for("index", error="Informe o e-mail e a senha."))

    # O DuckMail é a fonte de verdade para a autenticação; não pedimos token ao usuário.
    try:
        auth = requests.post(
            f"{DUCKMAIL_API}/token",
            json={"address": email, "password": password},
            timeout=10,
        )
    except requests.RequestException:
        return redirect(url_for("index", error="Não foi possível conectar ao DuckMail."))
    try:
        jwt_token = auth.json().get("token") if auth.status_code == 200 else None
    except ValueError:
        jwt_token = None
    if not jwt_token:
        return redirect(url_for("index", error="E-mail ou senha incorretos no DuckMail."))

    # Regista localmente a conta já autenticada para carregar mensagens nas próximas rotas.
    # O campo token é legado e interno; nunca é solicitado nem mostrado ao utilizador.
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM mailboxes WHERE email = %s", (email,))
        exists = cursor.fetchone()
        if exists:
            cursor.execute("UPDATE mailboxes SET password = %s WHERE email = %s", (password, email))
        else:
            cursor.execute(
                "INSERT INTO mailboxes (email, password, token, owner) VALUES (%s, %s, %s, %s)",
                (email, password, secrets.token_urlsafe(24), "admin1"),
            )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return redirect(url_for("index", error="Autenticado no DuckMail, mas não foi possível iniciar a sessão."))

    session["mailbox_email"] = email
    return redirect(url_for("mailbox", email=email))

@app.route("/mailbox/<email>")
def mailbox(email):
    if session.get("mailbox_email") != email:
        return redirect(url_for("index", error="Faça login para acessar a caixa de entrada."))
    mailboxes = get_all_mailboxes_global()
    acc_info = mailboxes.get(email)
    if not acc_info:
        session.pop("mailbox_email", None)
        return redirect(url_for("index", error="Conta não encontrada."))

    try:
        res_token = requests.post(
            f"{DUCKMAIL_API}/token",
            json={"address": email, "password": acc_info["password"]},
            timeout=10,
        )
        messages = []
        if res_token.status_code == 200:
            jwt_token = res_token.json().get("token")
            if jwt_token:
                res_msgs = requests.get(
                    f"{DUCKMAIL_API}/messages",
                    headers={"Authorization": f"Bearer {jwt_token}"},
                    timeout=10,
                )
                if res_msgs.status_code == 200:
                    msgs_data = res_msgs.json()
                    messages = msgs_data.get("hydra:member", msgs_data)
    except (requests.RequestException, ValueError):
        messages = []
    return render_template("mailbox.html", email=email, messages=messages)

@app.route("/message/<email>/<msg_id>")
def view_message(email, msg_id):
    if session.get("mailbox_email") != email:
        return redirect(url_for("index", error="Acesso negado. Faça login novamente."))
    mailboxes = get_all_mailboxes_global()
    acc_info = mailboxes.get(email)
    if not acc_info:
        session.pop("mailbox_email", None)
        return redirect(url_for("index", error="Conta não encontrada."))

    try:
        res_token = requests.post(
            f"{DUCKMAIL_API}/token",
            json={"address": email, "password": acc_info["password"]},
            timeout=10,
        )
        if res_token.status_code != 200 or not res_token.json().get("token"):
            return redirect(url_for("mailbox", email=email, error="Erro ao autenticar no DuckMail."))
        jwt_token = res_token.json()["token"]
        res_msg = requests.get(
            f"{DUCKMAIL_API}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {jwt_token}"},
            timeout=10,
        )
    except (requests.RequestException, ValueError):
        return redirect(url_for("mailbox", email=email, error="Erro de conexão com o DuckMail."))
    if res_msg.status_code != 200:
        return redirect(url_for("mailbox", email=email, error="Mensagem não encontrada."))

    msg_data = res_msg.json()
    subject = msg_data.get("subject", "Sem assunto")
    sender_data = msg_data.get("from", {})
    sender = sender_data.get("address", "Desconhecido") if isinstance(sender_data, dict) else str(sender_data)
    html_content = msg_data.get("html")
    if isinstance(html_content, list):
        html_content = "".join(html_content)
    if not html_content:
        from html import escape
        html_content = f"<pre>{escape(str(msg_data.get('text', 'Conteúdo vazio')))}</pre>"
    else:
        injected_styles = """
        <style>
            body { background: #fff !important; color: #111 !important; }
            img { max-width: 100% !important; height: auto !important; }
        </style>
        """
        if "<head>" in html_content:
            html_content = html_content.replace("<head>", f"<head>{injected_styles}", 1)
        else:
            html_content = injected_styles + html_content
    return render_template("message.html", email=email, subject=subject, sender=sender, html_content=html_content)

@app.route("/client-logout")
def client_logout():
    session.pop("mailbox_email", None)
    return redirect(url_for("index", msg="Sessão encerrada."))

@app.route("/admin")
def admin_shortcut():
    return redirect(url_for("admin_login"))

@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password")
        if password == ADMIN_1_PASSWORD:
            session["admin_logged"] = True
            session["admin_owner"] = "admin1"
            return redirect(url_for("admin_panel"))
        elif password == ADMIN_2_PASSWORD:
            session["admin_logged"] = True
            session["admin_owner"] = "admin2"
            return redirect(url_for("admin_panel"))
        # Usa o novo sistema de alerta flutuante de erro
        return redirect(url_for("admin_login", error="admin"))
    return render_template("admin_login.html")

@app.route("/admin-generate", methods=["POST"])
def admin_generate():
    if not session.get("admin_logged"):
        return redirect(url_for("admin_login"))
    
    current_owner = session.get("admin_owner", "admin1")
    
    try:
        res_dom = requests.get(f"{DUCKMAIL_API}/domains", timeout=10)
        if res_dom.status_code != 200:
            return redirect(url_for("admin_panel", error="Erro ao obter domínios da API."))
        domains_data = res_dom.json()
        domains = domains_data.get("hydra:member", domains_data)
        if not domains:
            return redirect(url_for("admin_panel", error="Nenhum domínio disponível."))
        domain_name = domains[0].get("domain") if isinstance(domains[0], dict) else domains[0]

        nomes = [
            "carlos", "lucas", "gabriel", "julia", "mateus", "ana", "bruno", 
            "pedro", "vitor", "beatriz", "rafael", "leonardo", "tiago", "carolina",
            "mariana", "fernando", "gustavo", "larissa", "roberto", "camila", "felipe"
        ]
        
        username = f"{random.choice(nomes)}{random.randint(1000, 9999)}"
        email = f"{username}@{domain_name}"
        password = secrets.token_hex(8)

        payload = {"address": email, "password": password}
        res_acc = requests.post(f"{DUCKMAIL_API}/accounts", json=payload, timeout=10)
        if res_acc.status_code not in [200, 201]:
            return redirect(url_for("admin_panel", error="Erro ao criar conta na API externa."))

        member_token = secrets.token_urlsafe(16)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO mailboxes (email, password, token, owner) VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO UPDATE SET password = EXCLUDED.password, token = EXCLUDED.token, owner = EXCLUDED.owner",
            (email, password, member_token, current_owner)
        )
        conn.commit()
        cursor.close()
        conn.close()

        return redirect(url_for("admin_panel", msg="E-mail gerado com sucesso!"))
    except Exception as e:
        return redirect(url_for("admin_panel", error=f"Erro técnico: {str(e)}"))

@app.route("/admin-delete", methods=["POST"])
def admin_delete():
    if not session.get("admin_logged"):
        return redirect(url_for("admin_login"))
    
    current_owner = session.get("admin_owner", "admin1")
    email_to_delete = request.form.get("email")
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM mailboxes WHERE email = %s AND owner = %s", (email_to_delete, current_owner))
        conn.commit()
        cursor.close()
        conn.close()
        return redirect(url_for("admin_panel", msg="E-mail excluído com sucesso!"))
    except Exception as e:
        print("Erro ao deletar do Supabase:", e)
        return redirect(url_for("admin_panel", error="Erro ao excluir e-mail."))

@app.route("/admin-panel")
def admin_panel():
    if not session.get("admin_logged"):
        return redirect(url_for("admin_login"))
    
    current_owner = session.get("admin_owner", "admin1")
    mailboxes = get_mailboxes_by_owner(current_owner)
    return render_template("admin_panel.html", mailboxes=mailboxes)

@app.route("/logout-admin")
def logout_admin():
    session.pop("admin_logged", None)
    session.pop("admin_owner", None)
    return redirect(url_for("admin_login"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
