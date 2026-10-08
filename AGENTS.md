# Doctor Top CPD — Voice Studio

Independent no-password voice studio for the owner’s friend. Read README.md first.

- The owner explicitly requested no password and authorized using their existing Gemini API key on 2026-10-08. Store the key only in Vercel server environment, never in Git or frontend.
- Keep a fresh SESSION_SECRET for signed anonymous upload sessions. No login/password UI. Browser sessions separate pending recordings; completed voices/history are shared within this deployment.
- Use a new independent private Vercel Blob store. Never copy original recordings, voice IDs, metadata, password, session secret or Blob token. Do not modify the original Voice Clone or student starter projects.
- Never enumerate original Gemini project profiles or accept unknown provider voice IDs through this public app. Only this installation's saved voices and intentionally supported built-in voices may be used.
- Preserve consent flow, upload limits, same-origin write checks and safe paths. Server credentials never enter API responses.
- Never commit `.env*` (except blank .env.example), .vercel, voices.json, samples/, out/, _mock/, .migration/, logs/, artifacts/, or private tokens.
- Use mock providers for tests; do not generate paid speech or create voice profiles for testing unless explicitly asked. A read-only provider readiness check is allowed.
- Run Python unit tests, Node mobile recorder tests and scripts/check_release.py before deploying.
- Production is this standalone Vercel project only; preserve existing unrelated deployments/data.
