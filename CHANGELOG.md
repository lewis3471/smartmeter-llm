# Changelog

## 1.4.2 — 2026-07-11

- Add HAOS add-on Git feedback sync configuration: repository URL, branch,
  protected deploy key, and sync interval.
- The add-on now clones into persistent `/data`, retrains there, and pushes
  evidence/models without requiring NUC shell or `sudo` access.

## 1.4.1 — 2026-07-11

- Train the LCD OCR per physical digit box: six kWh and five watt cells now
  retain their own references, while unseen box/digit combinations safely fall
  back to the global reference set.
- Retrained the shipped model from 541 clean local images (5,951 cells):
  99.73% holdout cell accuracy and 134/136 exact readings.
- Save each rejected read as a structured error event with its source JPEG.
- Add a NUC feedback worker and systemd timer to sync evidence, retrain from
  new Gemini-labelled images, commit the model, and push it through Git.
- Rotate Gemini models on HTTP 404 as well as rate-limit/service failures.
