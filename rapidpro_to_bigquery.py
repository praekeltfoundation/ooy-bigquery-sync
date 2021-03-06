import requests
import json
import os
from temba_client.v2 import TembaClient
from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core.exceptions import BadRequest

from datetime import datetime, timedelta

from fields import (
    CONTACT_FIELDS, GROUP_CONTACT_FIELDS, FLOWS_FIELDS,
    FLOW_RUNS_FIELDS, FLOW_RUN_VALUES_FIELDS, GROUP_FIELDS, PAGEVIEW_FIELDS)


BQ_KEY_PATH = os.environ.get('BQ_KEY_PATH', "credentials.json")
BQ_DATASET = "one2one-datascience.rapidpro"
RAPIDPRO_URL = "https://one2one.rapidpro.lvcthealth.org/"
RAPIDPRO_TOKEN = os.environ.get('RAPIDPRO_TOKEN', "")
CONTENTREPO_TOKEN = os.environ.get('CONTENTREPO_TOKEN', "")

credentials = service_account.Credentials.from_service_account_file(
    BQ_KEY_PATH, scopes=["https://www.googleapis.com/auth/cloud-platform"],
)

bigquery_client = bigquery.Client(
    credentials=credentials, project=credentials.project_id,
)

rapidpro_client = TembaClient(RAPIDPRO_URL, RAPIDPRO_TOKEN)

def log(text):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} - {text}")


def get_contact_wa_urn(contact):
    wa_urn = " "
    for rapidpro_urn in contact.urns:
        if "whatsapp" in rapidpro_urn:
            urn = rapidpro_urn.split(":")[1]
            wa_urn = f"+{urn}"
        else:
            wa_urn = "+"
    return wa_urn


def get_groups():
    rapidpro_groups = rapidpro_client.get_groups().all(retry_on_rate_exceed=True)

    groups = []
    for group in rapidpro_groups:
        groups.append({"uuid": group.uuid, "name": group.name})

    return groups


def get_contacts_and_contact_groups(last_contact_date=None):
    rapidpro_contacts = rapidpro_client.get_contacts(after=last_contact_date).all(
        retry_on_rate_exceed=True
    )

    contacts = []
    group_contacts = []
    for contact in rapidpro_contacts:
        record = {
            "uuid": contact.uuid,
            "modified_on": contact.modified_on.isoformat(),
            "name": contact.name,
            "urn": get_contact_wa_urn(contact),
        }

        for group in contact.groups:
            group_contacts.append(
                {"contact_uuid": contact.uuid, "group_uuid": group.uuid}
            )

        for field, value in contact.fields.items():
            if field in CONTACT_FIELDS.keys():
                record[field] = value

        contacts.append(record)

    return contacts, group_contacts


def get_last_record_date(table, field):
    query = f"select EXTRACT(DATETIME from max({field})) from {BQ_DATASET}.{table};"
    for row in bigquery_client.query(query).result():
        if row[0]:
            timestamp = row[0] + timedelta(hours=2)
            return str(timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


def get_flows():
    rapidpro_flows = rapidpro_client.get_flows().all(retry_on_rate_exceed=True)

    records = []
    for flow in rapidpro_flows:
        records.append(
            {
                "uuid": flow.uuid,
                "name": flow.name,
                "labels": [label.name for label in flow.labels],
            }
        )
    return records


def get_flow_runs(flows, last_contact_date=None):
    records = []
    value_records = []

    for flow in flows:
        for run_batch in rapidpro_client.get_runs(flow=flow["uuid"], after=last_contact_date).iterfetches(retry_on_rate_exceed=True):
            for run in run_batch:

                exited_on = None
                if run.exited_on:
                    exited_on = run.exited_on.isoformat()

                records.append(
                    {
                        "id": run.id,
                        "flow_uuid": run.flow.uuid,
                        "contact_uuid": run.contact.uuid,
                        "responded": run.responded,
                        "created_at": run.created_on.isoformat(),
                        "modified_on": run.modified_on.isoformat(),
                        "exited_on": exited_on,
                        "exit_type": run.exit_type,
                    }
                )

                for value in run.values.values():
                    value_records.append(
                        {
                            "run_id": run.id,
                            "value": str(value.value),
                            "category": value.category,
                            "time": value.time.isoformat(),
                            "name": value.name,
                            "input": value.input,
                        }
                    )

    return records, value_records


def get_content_repo_page_views(last_contact_date=None):
    records = []
    url = 'http://one2one.content.lvcthealth.org/api/v2/custom/pageviews/'
    headers = {'Authorization': 'token {}'.format(CONTENTREPO_TOKEN)}
    response = requests.get(url, headers=headers)
    results = json.loads(response.content)['results']
    for result in results:
        result_data = {
            "timestamp": result['timestamp'],
            "page": result['page'],
            "revision": result['revision'],
            "id": result['id'],
            "run_uuid": "",
            "contact_uuid": "",
        }
        if "run_uuid" and "contact_uuid" in result['data'].keys():
            result_data['run_uuid'] = result['data']['run_uuid']
            result_data['contact_uuid'] = result['data']['contact_uuid']
        records.append(result_data)
    return records



def upload_to_bigquery(table, data, fields):
    if table in ["flows", "groups"]:
        job_config = bigquery.LoadJobConfig(
            source_format="NEWLINE_DELIMITED_JSON",
            write_disposition="WRITE_TRUNCATE",
            max_bad_records=1,
            autodetect=False
        )
    else:
        schema = []
        for field, data_type in fields.items():
            schema.append(bigquery.SchemaField(field, data_type))

        job_config = bigquery.LoadJobConfig(
            source_format="NEWLINE_DELIMITED_JSON",
            write_disposition="WRITE_APPEND",
            max_bad_records=1,
            autodetect=False,
            schema=schema
        )

    job = bigquery_client.load_table_from_json(
        data, f"{BQ_DATASET}.{table}", job_config=job_config
    )
    try:
        job.result()
    except BadRequest as e:
        for e in job.errors:
            print('ERROR: {}'.format(e['message']))


if __name__ == "__main__":
    last_contact_date_contacts = get_last_record_date("contacts_raw", "modified_on")
    last_contact_date_flows = get_last_record_date("flow_runs", "created_at")
    last_contact_date_pageviews = get_last_record_date("page_views", "timestamp")
    fields = rapidpro_client.get_fields().all()
    log("Start")
    log("Fetching page views")
    pageviews = get_content_repo_page_views(last_contact_date_pageviews)
    log("Fetching flows")
    flows = get_flows()
    log("Fetching flow runs and values...")
    flow_runs, flow_run_values = get_flow_runs(flows, last_contact_date=last_contact_date_flows)
    log(f"flow_runs: {len(flow_runs)}")
    log(f"flow_run_values: {len(flow_run_values)}")
    log("Fetching groups...")
    groups = get_groups()
    log(f"Groups: {len(groups)}")
    log("Fetching contacts and contact groups...")
    contacts, group_contacts = get_contacts_and_contact_groups(last_contact_date=last_contact_date_contacts)
    log(f"Contacts: {len(contacts)}")
    log(f"Group Contacts: {len(group_contacts)}")

    tables = {
        "groups": {
            "data": groups,
            "fields": GROUP_FIELDS},
        "contacts_raw": {
            "data": contacts,
            "fields": CONTACT_FIELDS,
        },
        "group_contacts": {
            "data": group_contacts,
            "fields": GROUP_CONTACT_FIELDS,
        },
        "flows": {
            "data": flows,
            "fields": FLOWS_FIELDS,
        },
        "page_views": {
            "data": pageviews,
            "fields": PAGEVIEW_FIELDS,
        },
        "flow_runs": {
            "data": flow_runs,
            "fields": FLOW_RUNS_FIELDS,
        },
        "flow_run_values": {
            "data": flow_run_values,
            "fields": FLOW_RUN_VALUES_FIELDS,
        }
    }

    for table, data in tables.items():
        rows = data["data"]
        log(f"Uploading {len(rows)} {table}")

        upload_to_bigquery(table, rows, data.get("fields"))

    log("Done")
