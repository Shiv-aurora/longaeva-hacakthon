# Vercel hosted demo

Target project: `ual-twin`, production domain `ual-twin.vercel.app`, in
`shiv-aroras-projects-083eab8d`. Vercel imports `app.server:app` using Python 3.12.
Deploy from this application directory; the parent proposal PDFs and raw archives
are not deployment inputs. `.vercelignore` excludes credentials, environments,
temporary files and generated research stores.

The main monthly scenario, bounded failure/target grids and replay-verified thesis
export run on the server. This is the same numerical engine as the local app.
Hosted model-code receipts and exports use writable temporary storage. Thesis
PDF/JSON bytes return with the export response, so a download does not depend on
a later request reaching the same instance. Current source is registered before
replaying an export from another instance; incompatible code/environment receipts
still fail verification.

Download run records and thesis files to retain them. Hosted temporary files are
not a durable archive. Generated historical research caches are excluded and may
show unavailable; source declarations and the primary curated operating inputs
remain bundled. No missing research scores are fabricated.

Large repeated-seed convergence studies and shared quote uploads are local-only.
Hosted grids allow at most 250,000 path/point combinations; default 3×3 grids with
10,000 paths use 90,000. Function duration is bounded at 60 seconds. These hosting
limits do not change the financial assumptions or establish forecast calibration.

Before deployment, 58 API/archive/thesis/inverse-workflow tests and two new hosted
tests passed. The hosted tests erase the prior model archive, recreate it from
current captured source, replay the run, and check the returned one-page PDF and
matching JSON. They also check quote and grid limits.

Deploy the reviewed commit with Vercel CLI 63.1.2 and explicit account scope:

```bash
npx --yes vercel@63.1.2 --scope shiv-aroras-projects-083eab8d deploy --prod --skip-domain --yes
# Test the staged production URL, then promote that deployment.
npx --yes vercel@63.1.2 --scope shiv-aroras-projects-083eab8d promote DEPLOYMENT_URL --yes
```

The linked project/credentials remain ignored. Never include `.env.local` or
`.vercel` in source archives.
