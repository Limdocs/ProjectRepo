# Lambda Functions Flow

This document maps all Lambda functions in `backend/template.yaml` and how each one is triggered.

```mermaid
flowchart TD
    User[User / Frontend]
    APIGW[API Gateway + Cognito Authorizer]
    S3Raw[(S3 Raw Uploads Bucket)]
    S3Processed[(S3 Processed Outputs Bucket)]
    DDB[(DynamoDB Tables)]
    Textract[Amazon Textract]
    OpenAI[OpenAI API]

    User --> APIGW

    APIGW --> L1[create_user]
    APIGW --> L2[create_course]
    APIGW --> L3[get_courses]
    APIGW --> L4[generate_upload_url]
    APIGW --> L5[get_course_documents]
    APIGW --> L6[delete_document]
    APIGW --> L7[delete_course]
    APIGW --> L8[generate_questions api_handler]
    APIGW --> L9[get_questions]
    APIGW --> L10[submit_attempt]
    APIGW --> L11[get_course_attempts]
    APIGW --> L12[get_user_progress]
    APIGW --> L13[get_attempt_answers]
    APIGW --> L14[delete_attempt]

    L1 --> DDB
    L2 --> DDB
    L3 --> DDB
    L4 --> DDB
    L4 --> S3Raw
    L5 --> DDB
    L6 --> DDB
    L6 --> S3Raw
    L6 --> S3Processed
    L7 --> DDB
    L7 --> S3Raw
    L7 --> S3Processed
    L9 --> DDB
    L10 --> DDB
    L11 --> DDB
    L12 --> DDB
    L13 --> DDB
    L14 --> DDB

    S3Raw -- s3:ObjectCreated:* --> L15[process_document]
    L15 --> Textract
    L15 --> S3Processed
    L15 --> OpenAI
    L15 --> DDB

    L8 -- async invoke --> L16[generate_questions worker_handler]
    L8 --> DDB
    L16 --> DDB
    L16 --> S3Processed
    L16 --> OpenAI
```

## Trigger Summary

- **User-triggered via API Gateway:** all functions except `process_document` and `generate_questions worker_handler`.
- **Event-triggered:** `process_document` is triggered by S3 object creation in the raw uploads bucket.
- **Lambda-to-Lambda async trigger:** `generate_questions api_handler` invokes `generate_questions worker_handler` asynchronously.
