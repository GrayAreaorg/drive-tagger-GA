# Drive Tagger

Will crawl documents in a Google Drive folder and extract tags (anything starting with `#` in comments), then create a spreadsheet of the tagged text.

## Setup

- Install dependencies: `pip install click tqdm google-api-python-client google-auth-httplib2 google-auth-oauthlib`
- Place the provided `credentials.json` and `token.pickle` files in this folder.
- Ensure the Google Drive API and Google Sheets API are enabled for the associated Google Cloud project.
- Get the ID of the folder you want to crawl. Open the folder in Drive and look at the URL - the ID is the last part, i.e. `https://drive.google.com/drive/u/0/folders/<FOLDER_ID>`
- Create a spreadsheet that you want to populate with the tagged data. Get that ID, it's also in the URL: `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit#gid=0`

For unattended server use, `credentials.json` and an authorized `token.pickle` must be present in the same directory as the script.

Do not publish or commit `credentials.json` or `token.pickle` to a public repository.

## Usage

Synchronize spreadsheet with document tags:

```bash
python drive_tagger_registry.py sync FOLDER_ID SHEET_ID
```


### Registry / server mode

Drive Tagger can also sync multiple projects listed in a Google Sheet registry.

Run:

```bash
python drive_tagger_registry.py sync-all REGISTRY_SHEET_ID
```