const express = require('express');
const router = express.Router();
const pool = require('../db');

async function getKeycloakToken() {
  const res = await fetch('http://localhost:8080/realms/master/protocol/openid-connect/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'password',
      client_id: 'admin-cli',
      username: process.env.KEYCLOAK_ADMIN,
      password: process.env.KEYCLOAK_ADMIN_PASSWORD,
    })
  });
  const data = await res.json();
  return data.access_token;
}

async function syncUsers() {
  try {
    const token = await getKeycloakToken();
    const res = await fetch(`http://localhost:8080/admin/realms/${process.env.KEYCLOAK_REALM}/users`, {
      headers: { Authorization: `Bearer ${token}` }
    });
    const users = await res.json();
    // console.log(users);

    for (const user of users) {
      await pool.execute(
        `INSERT INTO users (keycloak_user_id, username, email, first_name, last_name)
         VALUES (?, ?, ?, ?, ?)
         ON DUPLICATE KEY UPDATE email = VALUES(email)`,
        [user.id, user.username, user.email || null, user.firstName || null, user.lastName || null]
      );
    }
    console.log(`✅ Synced ${users.length} users`);
  } catch (err) {
    console.error('❌ Sync error:', err.message);
  }
}

// Poll every 30 seconds
setInterval(syncUsers, 30000);
syncUsers(); // Run immediately on start

// GET /api/waitlist — view all entries
router.get('/waitlist', async (req, res) => {
  try {
    const [rows] = await pool.execute(
      'SELECT * FROM users ORDER BY registered_at DESC'
    );
    res.json(rows);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;