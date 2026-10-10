# shubh

Personal website for Shubham Anandani. A dependency-free static site, ready for deployment to Azure Static Web Apps or any static host.

## Local preview

Open `index.html` directly in a browser, or serve this folder with a static server:

```sh
npx serve .
```

## Deployment

GitHub Actions runs the dependency-free article build and validator before uploading the static-site artifact.

## Add an article

Put the article body in an HTML fragment file with semantic elements such as headings, paragraphs, lists, images with `alt` text, and `<pre><code>` blocks. Then run:

```sh
python3 scripts/article_tools.py new \
  --title "A useful article title" \
  --date "2026-10-10T12:00:00-07:00" \
  --tags "docker,containers" \
  --body /path/to/article.html
```

The date must be an ISO 8601 timestamp with its timezone offset. The command creates the data entry and article page from `templates/article.html`, then regenerates the feeds, writing archive, sitemap, and publishing logs. It derives the share summary from the first paragraph in the body. Use one to four lowercase alphanumeric tags, up to 20 characters each. Existing article publication days are stored at noon in `America/Los_Angeles`; the offset is computed for each date, including daylight saving time. Run `python3 scripts/article_tools.py validate` to check generated output without rebuilding it.
