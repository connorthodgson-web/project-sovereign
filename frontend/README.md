# Sovereign Operator Console

Prototype web dashboard for Project Sovereign.

```powershell
npm install
npm run dev
```

The app uses mock dashboard data by default and probes the local backend at `http://127.0.0.1:8000` for `/health` and `/chat`.

Set `VITE_SOVEREIGN_API_URL` to point at a different backend.
