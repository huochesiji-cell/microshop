import os
from flask import Flask, jsonify
import redis

app = Flask(__name__)

def get_redis():
    return redis.Redis(
        host=os.environ.get('REDIS_HOST', 'redis'),
        port=int(os.environ.get('REDIS_PORT', 6379)),
        decode_responses=True,
    )

@app.route('/api/users')
def list_users():
    try:
        r = get_redis()
        # 每次访问计数 +1，证明 Redis 缓存/计数生效
        r.incr('visit_count')
        count = int(r.get('visit_count'))
        users = [
            {"id": 1, "name": "张三"},
            {"id": 2, "name": "李四"},
            {"id": 3, "name": "王五"},
        ]
        return jsonify({"code": 0, "data": users, "visit_count": count})
    except Exception as e:
        return jsonify({"code": 1, "error": str(e)}), 500

@app.route('/healthz')
def healthz():
    return "ok"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
