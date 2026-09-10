import re
import html
import click
import pickle
import os.path
from tqdm import tqdm
from collections import defaultdict, namedtuple
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from datetime import datetime

TAG_RE = re.compile('#[A-Za-z0-9-_]+')

# If modifying these scopes, delete the file token.pickle.
SCOPES = [
    'https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets'
]

Comment = namedtuple('Comment', ['id', 'highlighted', 'text', 'tags', 'user', 'url'])

class Drive:
    def __init__(self):
        creds = None

        # The file token.pickle stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first
        # time.
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)

        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json', SCOPES)
                creds = flow.run_local_server()

            # Save the credentials for the next run
            with open('token.pickle', 'wb') as token:
                pickle.dump(creds, token)

        self.service = build('drive', 'v3', credentials=creds)
        self.sheets = build('sheets', 'v4', credentials=creds).spreadsheets()

    def list_folder(self, folder_id):
        """List documents in a folder"""
        params = {
            'q': '"{}" in parents'.format(folder_id),
            'fields': '*'
        }
        resp = self.service.files().list(**params).execute()
        files = resp['files']
        while 'nextPageToken' in resp:
            page = resp['nextPageToken']
            resp = self.service.files().list(pageToken=page, **params).execute()
            files += resp['files']


        # Limit to docs
        return [f for f in files if f['mimeType'] == 'application/vnd.google-apps.document']

    def get_folder_tags(self, folder_id):
        """Get tagged text and tags for
        all documents in a folder"""
        tags = []
        doc_meta = {}
        doc_graph = defaultdict(list)
        com_graph = defaultdict(list)

        files = self.list_folder(folder_id)

        for f in tqdm(files):
            doc_id = f['id']
            file = self.service.files().get(fileId=doc_id).execute()
            doc_meta[doc_id] = {
               'title': file['name']
            }

            tagged = self.get_doc_tags(doc_id)

            # Document -> Tag edges:
            # one edge for every tag occurrence
            for com in tagged:
                for tag in com.tags:
                    doc_graph[doc_id].append(tag)

            # Tag -> Tag edges:
            # one edge per pair of tags that co-occur in this document
            document_tags = sorted(set(
                tag
                for com in tagged
                for tag in com.tags
            ))

            for i, tag_a in enumerate(document_tags):
                for tag_b in document_tags[i + 1:]:
                    com_graph[tag_a].append(tag_b)

            tags += [(doc_id, com) for com in tagged]

        return tags, (doc_graph, com_graph), doc_meta


    def get_doc_tags(self, document_id):
        """Get tagged text and tags for a document"""
        # file = self.service.files().get(fileId=document_id).execute()
        # rev = self.service.revisions().get(fileId=document_id, revisionId='head').execute()
        doc_url = 'https://docs.google.com/document/d/{}/edit'.format(document_id)

        # Fetch all comments
        params = {
            'fileId': document_id,
            'includeDeleted': False,
            'fields': '*'
        }
        resp = self.service.comments().list(**params).execute()
        comments = resp['comments']
        while 'nextPageToken' in resp:
            page = resp['nextPageToken']
            resp = self.service.comments().list(pageToken=page, **params).execute()
            comments += resp['comments']

        # Don't include resolved comments
        comments = [c for c in comments if not c.get('resolved', False)]

        # Extract tags
        tagged = []
        for comment in comments:
            highlighted = comment['quotedFileContent']['value']
            highlighted = html.unescape(highlighted)

            # Get tags from main comment and replies
            for com in [comment] + comment['replies']:
                # Standardize tags to lowercase
                tags = [t.strip('#').lower() for t in TAG_RE.findall(com['content'])]
                if not tags: continue

                text = com['content']
                user = com['author']['displayName']
                comment_url = '{}?disco={}'.format(doc_url, com['id'])

                id = '{}#{}'.format(document_id, com['id'])
                tagged.append(Comment(id, highlighted, text, tags, user, comment_url))
        return tagged

    def create_sheet(self, sheet_id, title):
        requests = [{
            'addSheet': {
                'properties': {
                    'title': title
                }
            }
        }]
        body = {'requests': requests}
        resp = self.sheets.batchUpdate(spreadsheetId=sheet_id, body=body).execute()
        return resp['replies'][0]['addSheet']['properties']

    def update_sheet(self, sheet_id, sheets, index, title, headers, values):
        try:
            sheet = next(s for s in sheets if s['index'] == index)
        except StopIteration:
            # Create if necessary
            sheet = self.create_sheet(sheet_id, title)
        needed_rows = len(values) + 1
        current_rows = sheet.get('gridProperties', {}).get('rowCount', 1000)

        if needed_rows > current_rows:
            resize_body = {
                'requests': [{
                    'updateSheetProperties': {
                        'properties': {
                            'sheetId': sheet['sheetId'],
                            'gridProperties': {
                                'rowCount': needed_rows
                            }
                        },
                        'fields': 'gridProperties.rowCount'
                    }
                }]
            }
            self.sheets.batchUpdate(
                spreadsheetId=sheet_id,
                body=resize_body
            ).execute()

        requests = [{
            'updateCells': {
                'rows': [{
                    'values': [{
                        'userEnteredValue': {
                            'stringValue': c
                        }
                    } for c in m]
                } for m in [headers] + values],
                'range': {
                    'sheetId': sheet['sheetId']
                },
                'fields': 'userEnteredValue'
            }
        }, {
            'updateSheetProperties': {
                'properties': {
                    'sheetId': sheet['sheetId'],
                    'title': title
                },
                'fields': 'title'
            }

        }]
        body = {'requests': requests}
        resp = self.sheets.batchUpdate(spreadsheetId=sheet_id, body=body).execute()

    def update_spreadsheet(self, sheet_id, tagged, graphs, doc_meta, top_tags=0):
        # Get sub-sheets
        resp = self.sheets.get(spreadsheetId=sheet_id).execute()
        sheets = [s['properties'] for s in resp['sheets']]

        # Reset sheets
        requests = [{
            'updateCells': {
                'range': {
                    'sheetId': s['sheetId']
                },
                'fields': 'userEnteredValue'
            }
        } for s in sheets]
        body = {'requests': requests}
        self.sheets.batchUpdate(spreadsheetId=sheet_id, body=body).execute()

        # Count documents tags appear in and total tag occurrences
        tag_documents = defaultdict(set)
        tag_occurrences = defaultdict(int)

        for doc_id, com in tagged:
            for tag in com.tags:
                tag_documents[tag].add(doc_id)
                tag_occurrences[tag] += 1

        tag_counts = {
            tag: len(docs)
            for tag, docs in tag_documents.items()
        }

        # Update first sheet (tag summary)
        headers = ['Tag', '# Documents', '# Occurrences']
        values = [
            [tag, tag_counts[tag], tag_occurrences[tag]]
            for tag in tag_counts
        ]

        # Name the first sheet
        first_sheet = next(s for s in sheets if s['index'] == 0)
        self.sheets.batchUpdate(
            spreadsheetId=sheet_id,
            body={
                'requests': [{
                    'updateSheetProperties': {
                        'properties': {
                            'sheetId': first_sheet['sheetId'],
                            'title': 'Tag Summary'
                        },
                        'fields': 'title'
                    }
                }]
            }
        ).execute()

        body = {
            'values': [headers] + values
        }

        range = '1:{}'.format(len(body['values']))
        self.sheets.values().update(
            spreadsheetId=sheet_id,
            body=body,
            range=range,
            valueInputOption='RAW'
        ).execute()

        # Update second sheet (all tags)
        headers = ['Document ID', 'Title', 'Highlighted', 'Tags', 'Comment', 'User', 'Url']
        values = [[doc_id, doc_meta[doc_id]['title'], com.highlighted, ', '.join(com.tags), com.text, com.user, com.url]
                  for doc_id, com in tagged]
        self.update_sheet(sheet_id, sheets, 1, 'All Tags', headers, values)

        # Update graph sheets
        doc_graph, tag_graph = graphs

        # Document -> Tag graph
        values = []
        for doc_id, doc_tags in doc_graph.items():
            values += [[doc_id, tag] for tag in doc_tags]

        self.update_sheet(
            sheet_id,
            sheets,
            2,
           'Document->Tag Graph',
           ['Document ID', 'Tag'],
           values
        )

        # Tag -> Tag co-occurrence graph
        values = []
        for tag_a, related_tags in tag_graph.items():
            values += [[tag_a, tag_b] for tag_b in related_tags]

        self.update_sheet(
            sheet_id,
            sheets,
            3,
           'Tag->Tag Graph',
           ['Tag A', 'Tag B'],
           values
        )



        # Create per-tag sheets
        tag_groups = defaultdict(list)
        for doc_id, com in tagged:
            for tag in com.tags:
                tag_groups[tag].append((doc_id, com.highlighted))

        selected_tags = [
          	 tag
           	 for tag, _ in sorted(
           	    tag_counts.items(),
           	     key=lambda item: item[1],
           	     reverse=True
          	  )[:top_tags]
       		 ] if top_tags > 0 else []

        # Remove per-tag sheets that are not currently selected
        to_delete = [
            s for s in sheets
            if s['index'] not in [0, 1, 2, 3]
            and s['title'] not in selected_tags
        ]

        if to_delete:
            delete_requests = [{
                'deleteSheet': {
                    'sheetId': s['sheetId']
                }
            } for s in to_delete]

            self.sheets.batchUpdate(
                spreadsheetId=sheet_id,
                body={'requests': delete_requests}
            ).execute()

            # Refresh metadata after deleting sheets
            resp = self.sheets.get(spreadsheetId=sheet_id).execute()
            sheets = [s['properties'] for s in resp['sheets']]



                # Create any missing per-tag sheets in one batch
        existing_titles = {s['title'] for s in sheets}
        missing_tags = [tag for tag in selected_tags if tag not in existing_titles]

        if missing_tags:
            create_request = [{
    		'addSheet': {
       			 'properties': {
           			 'title': tag,
           			 'gridProperties': {
                			'rowCount': max(len(tag_groups[tag]), 1),
                				'columnCount': 2
            }
        }
    }
} for tag in missing_tags]

            self.sheets.batchUpdate(
                spreadsheetId=sheet_id,
                body={'requests': create_request}
            ).execute()

            # Refresh sheet metadata so we have the IDs of newly created sheets
            resp = self.sheets.get(spreadsheetId=sheet_id).execute()
            sheets = [s['properties'] for s in resp['sheets']]

        # Populate per-tag sheets
        sheets_by_title = {s['title']: s for s in sheets}
        sheet_requests = []

        for tag in tqdm(selected_tags):
            mentions = tag_groups[tag]
            sheet = sheets_by_title[tag]

            sheet_requests.append({
                'updateCells': {
                    'rows': [{
                        'values': [{
                            'userEnteredValue': {
                                'stringValue': c
                            }
                        } for c in m]
                    } for m in mentions],
                    'range': {
                        'sheetId': sheet['sheetId']
                    },
                    'fields': 'userEnteredValue'
                }
            })

        if sheet_requests:
            body = {'requests': sheet_requests}
            self.sheets.batchUpdate(
                spreadsheetId=sheet_id,
                body=body
            ).execute()

def extract_folder_id(value):
    """Extract a Google Drive folder ID from a URL, or accept a bare ID."""
    value = value.strip()
    match = re.search(r'/folders/([A-Za-z0-9_-]+)', value)

    if match:
        return match.group(1)

    if '/' not in value:
        return value

    raise ValueError('Could not extract a Drive folder ID from the supplied URL.')


def extract_sheet_id(value):
    """Extract a Google Sheets ID from a URL, or accept a bare ID."""
    value = value.strip()
    match = re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)', value)

    if match:
        return match.group(1)

    if '/' not in value:
        return value

    raise ValueError('Could not extract a spreadsheet ID from the supplied URL.')


def is_active(value):
    """Return True when a registry row is activated."""
    normalized = re.sub(r'[^a-z0-9]+', '', str(value).strip().lower())

    return normalized in {
        'true',
        'yes',
        'y',
        '1',
        'active',
        'activate',
        'on'
    }


def parse_top_tags(value):
    """Blank means zero per-tag sheets."""
    value = str(value).strip()

    if not value:
        return 0

    return max(0, int(value))


def column_letter(index):
    """Convert a zero-based column index to an A1 column letter."""
    index += 1
    result = ''

    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result

    return result

@click.group()
def main():
    pass

@main.command()
@click.argument('folder_id')
@click.argument('sheet_id')
@click.option(
    '--top-tags',
    default=0,
    type=int,
    show_default=True,
    help='Create separate sheets for the N most common tags. Use 0 for none.'
)
def sync(folder_id, sheet_id, top_tags):
    drive = Drive()

    print('Reading comments...')
    tags, graphs, doc_meta = drive.get_folder_tags(folder_id)

    print('Updating spreadsheet...')
    drive.update_spreadsheet(sheet_id, tags, graphs, doc_meta, top_tags)
    print('Done')

@main.command('sync-all')
@click.argument('registry_sheet_id')
@click.option(
    '--tab',
    default=None,
    help='Registry sheet tab name. Defaults to the first tab.'
)
def sync_all(registry_sheet_id, tab):
    """Sync all active projects listed in the registry spreadsheet."""

    drive = Drive()

    # Identify registry tab
    metadata = drive.sheets.get(
        spreadsheetId=registry_sheet_id
    ).execute()

    if tab is None:
        tab = metadata['sheets'][0]['properties']['title']

    escaped_tab = tab.replace("'", "''")

    # Read registry
    response = drive.sheets.values().get(
        spreadsheetId=registry_sheet_id,
        range="'{}'!A:ZZ".format(escaped_tab)
    ).execute()

    rows = response.get('values', [])

    if not rows:
        print('Registry is empty.')
        return

    headers = rows[0]

    header_map = {
        header.strip(): index
        for index, header in enumerate(headers)
        if header.strip()
    }

    required_headers = [
        'Project',
        'Folder URL',
        'Sheet URL',
        'Top Tags',
        'Active',
        'Last Sync',
        'Last Successful Sync',
        'Status',
        'Last Error'
    ]

    missing_headers = [
        header
        for header in required_headers
        if header not in header_map
    ]

    if missing_headers:
        raise ValueError(
            'Missing registry columns: {}'.format(
                ', '.join(missing_headers)
            )
        )

    project_col = header_map['Project']
    folder_col = header_map['Folder URL']
    sheet_col = header_map['Sheet URL']
    top_tags_col = header_map['Top Tags']
    active_col = header_map['Active']

    last_sync_col = header_map['Last Sync']
    last_success_col = header_map['Last Successful Sync']
    status_col = header_map['Status']
    last_error_col = header_map['Last Error']

    def get_value(row, column):
        if column < len(row):
            return row[column]
        return ''

    for row_number, row in enumerate(rows[1:], start=2):

        if not is_active(get_value(row, active_col)):
            continue

        project = get_value(row, project_col).strip()
        if not project:
            project = 'Row {}'.format(row_number)

        print('\n=== {} ==='.format(project))

        last_sync = datetime.now().astimezone().isoformat(
            timespec='seconds'
        )

        last_success = None

        try:
            folder_id = extract_folder_id(
                get_value(row, folder_col)
            )

            output_sheet_id = extract_sheet_id(
                get_value(row, sheet_col)
            )

            top_tags = parse_top_tags(
                get_value(row, top_tags_col)
            )

            print('Reading comments...')
            tags, graphs, doc_meta = drive.get_folder_tags(folder_id)

            print('Updating spreadsheet...')
            drive.update_spreadsheet(
                output_sheet_id,
                tags,
                graphs,
                doc_meta,
                top_tags
            )

            status = 'OK'
            error = ''

            last_success = datetime.now().astimezone().isoformat(
                timespec='seconds'
            )

            print('Done')

        except Exception as exc:
            status = 'ERROR'
            error = '{}: {}'.format(
                type(exc).__name__,
                str(exc)
            )

            # Prevent extremely long exception text from filling the registry
            error = error[:5000]

            print('ERROR: {}'.format(error))

        updates = {
            last_sync_col: last_sync,
            status_col: status,
            last_error_col: error
        }

        if last_success:
            updates[last_success_col] = last_success

        data = []

        for column, value in updates.items():
            cell = '{}{}'.format(
                column_letter(column),
                row_number
            )

            data.append({
                'range': "'{}'!{}".format(
                    escaped_tab,
                    cell
                ),
                'values': [[value]]
            })

        drive.sheets.values().batchUpdate(
            spreadsheetId=registry_sheet_id,
            body={
                'valueInputOption': 'RAW',
                'data': data
            }
        ).execute()


if __name__ == '__main__':
    main()