require('dotenv').config({ path: __dirname + '/.env' });
const express = require('express');
const cors = require('cors');
const waitlistRoutes = require('./routes/waitlist');

const app = express();

app.use(cors({
  origin: 'http://localhost:5173' // Your Vue dev server
}));
app.use(express.json());

app.use('/api', waitlistRoutes);

app.get('/health', (_, res) => res.json({ status: 'ok' }));

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => {
  console.log(`🚀 Backend running at http://localhost:${PORT}`);
});