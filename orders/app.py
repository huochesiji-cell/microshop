import os
from flask import Flask, jsonify
import pymysql

app = Flask(__name__)

def get_conn():
    # 数据库连接信息全部从环境变量读取（由 K8s ConfigMap/Secret 注入）
    return pymysql.connect(
        host=os.environ.get('DB_HOST', 'mysql'),
        port=int(os.environ.get('DB_PORT', 3306)),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', ''),
        database=os.environ.get('DB_NAME', 'microshop'),
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
    )

@app.route('/api/orders')
def list_orders():
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT id, product, quantity, created_at FROM orders ORDER BY id DESC LIMIT 10")
            rows = cur.fetchall()
        conn.close()
        for r in rows:
            r['created_at'] = str(r['created_at'])
        return jsonify({"code": 0, "data": rows})
    except Exception as e:
        return jsonify({"code": 1, "error": str(e)}), 500

@app.route('/healthz')
def healthz():
    return "ok"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
