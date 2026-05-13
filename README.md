# Limdocs

**Transforming Raw Academic Materials into Interactive AI-Powered Learning Experiences.**

AWS Serverless
React Vite
OpenAI LLM
License MIT

---

## Premium UX/UI

Limdocs is designed as a modern academic product experience inspired by:

- **Apple-style minimalism** for visual clarity and focus
- **Stripe-level cleanliness** for structure, spacing, and hierarchy
- **Instagram-like navigation fluency** for intuitive, high-frequency workflows

Screenshots (placeholders):

- `[Dashboard View]`
- `[Course Space - RTL Support]`
- `[AI Processing Status]`

---

## The Core Mission

Limdocs helps students convert fragmented study materials into structured, actionable learning flows.  
Users manage courses, upload files securely, run automated AI extraction pipelines, and receive real-time processing feedback that turns static content into usable study assets.

---

## Key Features

### 1) Bilingual Mastery

- Full **RTL/LTR** experience for **Hebrew/English**
- Persisted language preference across sessions
- Consistent localization across auth, dashboard, and course workflows

### 2) AI-Driven Pipeline

- Automated document analysis powered by **Amazon Textract**
- Asynchronous extraction flow for large/scanned academic files
- Processing-state lifecycle management (`UPLOADED`, `EXTRACTED`, `FAILED`)

### 3) Smart Course Management

- Dedicated **Course Space** per course
- Structured materials list with metadata and status visibility
- Foundation for generated practice sets and adaptive study loops

### 4) Real-time Feedback

- Live polling on course documents while processing is in progress
- Automatic UI refresh when statuses transition to final states
- No page reload required for status visibility

---

## Technical Architecture (High-Level Design)

Limdocs uses a **serverless, event-driven architecture** optimized for scalability, operational simplicity, and cost efficiency.

### Serverless Stack

- **AWS Lambda** for backend business logic and orchestration
- **Amazon API Gateway** as authenticated REST entrypoint
- **Amazon DynamoDB** for low-latency NoSQL metadata and state management
- **Amazon S3** for raw uploads and processed outputs
- **AWS Cognito** for authentication and session-backed authorization

### Secure File Handling

Limdocs uses **S3 Pre-signed URLs** for direct-to-cloud uploads:

- keeps the upload bucket private
- offloads file transfer from backend compute
- improves throughput and reduces API/Lambda overhead

### Event-Driven AI Pipeline

1. User uploads file to S3 (raw bucket)
2. S3 event triggers processing Lambda
3. Lambda starts asynchronous Textract job
4. Orchestration polls Textract job status and paginates all result pages
5. Extracted text is persisted to processed outputs bucket
6. DynamoDB document state is updated for frontend visibility

This design provides **Asynchronous Orchestration** with resilient status tracking for long-running academic documents.

### Data Integrity and Cloud Hygiene

Limdocs implements **Cascading Deletion** with **S3-first-then-DB** semantics:

- delete raw and processed S3 objects first
- then delete DynamoDB metadata records
- prevents orphan files and stale metadata drift
- improves long-term storage hygiene and operational correctness

### Access Pattern-Oriented Data Modeling

DynamoDB design follows **access-pattern-driven modeling** with **GSI optimization** for course-centric queries (for example: fetch all documents by `course_id` efficiently).

---

## Technology Stack

- **Frontend:** React 18, Vite, CSS3 (including logical properties for RTL/LTR support)
- **Backend:** Python 3.12, AWS Lambda, AWS SAM (Infrastructure as Code)
- **Auth:** AWS Cognito
- **AI/OCR:** Amazon Textract (Async API)
- **Data:** Amazon DynamoDB, Amazon S3
- **API:** Amazon API Gateway

---

## Installation & Local Development

## 1) Frontend

```bash
cd frontend
npm install
npm run dev
```

Optional local env:

```bash
VITE_API_URL=https://<api-id>.execute-api.<region>.amazonaws.com/prod
```

## 2) Backend (SAM)

```bash
cd backend
sam validate
sam build
sam deploy --guided
```

Recommended prerequisites:

- Node.js 18+
- Python 3.12
- AWS CLI configured (`aws configure`)
- SAM CLI installed

---

## Future Roadmap

- **Phase 4:** LLM-based Question Generation (OpenAI)
- **Phase 5:** Student Progress Analytics and Interactive Quizzes

---

## Authors

- Jordan
- Nadav

---

## Documentation

- Design document: `docs/design.md`
- Engineering progress log: `docs/progress.log.md`

