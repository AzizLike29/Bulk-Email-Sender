import sqlite3

con = sqlite3.connect("audience.sqlite3")
cur = con.cursor()
cur.execute("SELECT id, email, name, status, created_at FROM subscribers WHERE status='active'")
rows = cur.fetchall()

for row in rows:
    print(row)

con.close()

# Run python cek_subscriber.py