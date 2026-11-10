/* Published alongside the hosted artifact in place of a real pool.js.
   A hosted artifact is sandboxed to its own origin, so it cannot reach a
   Supabase project however the key is supplied; it uses the artifact's own
   shared store instead. Run `ah.py ui` for the Supabase-backed pool. */
window.POOL = null;
