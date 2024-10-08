from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.providers.google.cloud.hooks.pubsub import PubSubHook
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook
from google.cloud import bigquery
from datetime import datetime, timedelta
import json

PROJECT_ID = 'spry-tesla-426720-e7'
PUBSUB_TOPIC = 'your-pubsub-topic'
PUBSUB_SUBSCRIPTION = 'your-pubsub-subscription'
DATASET_ID = 'gcs_bq_dataset_load'
TABLE_ID = 'incrementally_loaded_table'
WATERMARK_TABLE = 'watermark_table'

default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

def get_last_watermark():
    bq_hook = BigQueryHook(gcp_conn_id='google_cloud_default')
    query = f"""
    SELECT MAX(last_processed_timestamp) as last_watermark
    FROM `{PROJECT_ID}.{DATASET_ID}.{WATERMARK_TABLE}`
    """
    result = bq_hook.get_first(query)
    return result[0] if result and result[0] else datetime(1970, 1, 1).timestamp()

def update_watermark(timestamp):
    bq_hook = BigQueryHook(gcp_conn_id='google_cloud_default')
    client = bq_hook.get_client()
    table_ref = client.dataset(DATASET_ID).table(WATERMARK_TABLE)
    
    rows_to_insert = [{
        'last_processed_timestamp': timestamp,
        'update_time': datetime.now().isoformat()
    }]
    
    errors = client.insert_rows_json(table_ref, rows_to_insert)
    if errors:
        raise Exception(f"Error updating watermark: {errors}")
def detect_fraud(transaction):
    """
    Implement fraud detection rules
    """
    # Rule 1: Flag high-value transfers
    if transaction['type'] == 'TRANSFER' and transaction['amount'] > 200000:
        return True
    
    # Rule 2: Flag transactions that empty an account
    if transaction['newbalanceOrig'] == 0 and transaction['oldbalanceOrg'] > 0:
        return True
    
    # Rule 3: Flag rapid succession of small transfers
    if transaction['type'] == 'TRANSFER' and transaction['amount'] < 10:
        # This would require checking against recent transactions, 
        # which is not implemented in this simple example
        pass
    
    # Rule 4: Flag mismatched transaction amounts
    if abs(transaction['oldbalanceOrg'] - transaction['newbalanceOrig'] - transaction['amount']) > 0.01:
        return True

    return False

def process_transaction(transaction):
    """
    Process a single transaction and detect fraud
    """
    # Convert string values to appropriate types
    transaction['amount'] = float(transaction['amount'])
    transaction['oldbalanceOrg'] = float(transaction['oldbalanceOrg'])
    transaction['newbalanceOrig'] = float(transaction['newbalanceOrig'])
    transaction['oldbalanceDest'] = float(transaction['oldbalanceDest'])
    transaction['newbalanceDest'] = float(transaction['newbalanceDest'])
    
    # Detect fraud
    transaction['detected_fraud'] = detect_fraud(transaction)
    
    return transaction

def pull_and_process_messages(**context):
    pubsub_hook = PubSubHook(gcp_conn_id='google_cloud_default')
    last_watermark = get_last_watermark()
    
    messages = pubsub_hook.pull(
        PROJECT_ID, PUBSUB_SUBSCRIPTION, 
        max_messages=100, return_immediately=True
    )
    
    processed_data = []
    new_watermark = last_watermark
    for message in messages:
        data = json.loads(message.data.decode('utf-8'))
        message_timestamp = data.get('step', 0)  # Using 'step' as timestamp
        
        if message_timestamp > last_watermark:
            processed_transaction = process_transaction(data)
            processed_data.append(processed_transaction)
            new_watermark = max(new_watermark, message_timestamp)
        
        pubsub_hook.acknowledge(PROJECT_ID, PUBSUB_SUBSCRIPTION, [message.ack_id])
    
    if new_watermark > last_watermark:
        update_watermark(new_watermark)
    
    return processed_data

def insert_to_bigquery(**context):
    bq_hook = BigQueryHook(gcp_conn_id='google_cloud_default')
    client = bq_hook.get_client()
    table_ref = client.dataset(DATASET_ID).table(TABLE_ID)
    
    # Get processed data from previous task
    processed_data = context['task_instance'].xcom_pull(task_ids='pull_pubsub_messages')
    
    if not processed_data:
        print("No new data to insert.")
        return
    
    # Prepare the rows for insertion
    rows_to_insert = [bigquery.Row(data) for data in processed_data]
    
    # Insert rows
    errors = client.insert_rows(table_ref, rows_to_insert)
    
    if errors:
        raise Exception(f"Errors inserting rows: {errors}")
    else:
        print(f"Successfully inserted {len(rows_to_insert)} rows.")

with DAG(
    'pubsub_to_bigquery_fraud_detection',
    default_args=default_args,
    description='Incrementally load data from Pub/Sub to BigQuery with fraud detection',
    schedule_interval=timedelta(minutes=5),
    start_date=datetime(2023, 1, 1),
    catchup=False
) as dag:

    pull_and_process_task = PythonOperator(
        task_id='pull_and_process_messages',
        python_callable=pull_and_process_messages,
        provide_context=True,
    )

    insert_task = PythonOperator(
        task_id='insert_to_bigquery',
        python_callable=insert_to_bigquery,
        provide_context=True,
    )

    pull_and_process_task >> insert_task
