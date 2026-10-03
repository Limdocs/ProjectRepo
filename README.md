# Limdocs

**Turn course materials into AI-generated practice quizzes and track what you need to study next.**

Limdocs is an adaptive learning platform for students: create course spaces, upload lecture PDFs and slides, extract text automatically, generate exam-style question sets with explanations, practice in the app, and review performance by topic over time.

| | |
|---|---|
| **Frontend** | React 19, Vite, React Router, AWS Amplify Auth, AWS Amplify Hosting (GitHub-connected deploys) |
| **Backend** | Python 3.9 on AWS Lambda (SAM), API Gateway, Cognito |
| **AI / OCR** | Amazon Textract, OpenAI (`gpt-4.1-mini`) |
| **Data** | DynamoDB, S3 |

---

## What you can do today

- **Sign up and sign in** with AWS Cognito (email/username), including account confirmation; sync profile via `POST /users` after signup
- **Switch Hebrew ↔ English** with persisted preference and full RTL/LTR layout
- **Create and manage courses** from the dashboard (private or public visibility stored on the course; owners can change visibility from the course page via `PATCH /courses/{courseId}/visibility`; **browse public courses** on the Home **Explore Courses** tab via `GET /courses/public`, which follows the `visibility_courses_index` GSI; foreign public cards show the creator username and open the course in read-only mode)
- **Upload materials** via S3 pre-signed URLs (PDF, PNG, JPEG; **20 MB** max per file via `file_size_bytes`)
- **Process documents** asynchronously: S3 `uploads/` trigger → Textract → bilingual sub-topic extraction (OpenAI) → `READY` with topic chips on the Materials tab
- **Generate question sets** from one or more `READY` documents: async API + worker Lambdas, UI polling the current `generation_id` until that generation is `READY` or `FAILED`, optional **5/10/15/20** questions, **Hebrew or English** quiz language, and **weakness-focused** mode that prioritizes your weakest topics from `user_progress`
- **Take quizzes**, submit attempts (graded server-side), and review correct answers with explanations
- **Browse attempt history**, reopen past submissions in read-only review mode, and **delete** individual attempts (progress matrix deltas are reversed)
- **View weakness analytics** on the course **Analyzed Weaknesses** tab: weighted topic scores (Easy/Medium/Hard), weak/medium/strong status bands, vertical score chart, and per-topic breakdown—fed by quiz submissions and served from `GET /courses/{courseId}/progress`
- **Home dashboard** shows live per-course stats (document count, relative last activity, progress bar from averaged topic mastery) while stub nav items (global search, Documents / Analytics) remain presentational
- **Delete** documents, attempts, question sets, or entire courses (cascading S3 + DynamoDB cleanup, including `user_progress` on course delete)

**Course page tabs:** Materials, Question Sets, Attempts, Analyzed Weaknesses (deep-link with `?tab=questionSets` or `?tab=weaknesses`). Visitors on a public course see Materials (filenames only) and Question Sets (view/read only); Attempts and Analyzed Weaknesses stay owner-only.

---

## Architecture

Serverless, event-driven design on AWS:

```
Browser (React SPA)
    → AWS Amplify Hosting (GitHub-connected CI/CD)
    → Cognito (auth) + API Gateway (REST, Cognito authorizer)
        → Lambda handlers (courses, uploads, quizzes, attempts, progress)
    → S3 raw uploads (pre-signed PUT under uploads/) → S3 event → process_document Lambda
        → Textract (async) → processed text in S3 → OpenAI topic extraction → DynamoDB READY
    → POST /generate-quiz → generate_questions api_handler (202)
        → async invoke → generate_questions worker_handler → question_sets + questions tables
    → submit attempt → grades answers → attempts + attempt_answers + user_progress matrix (topic × difficulty)
    → delete attempt → subtract matrix_deltas (rebuild fallback on drift)
```

Course-scoped APIs use `course_access.resolve_course_access` (Cognito `sub` vs `owner_id`, plus `PUBLIC` for logged-in visitors). Mutations, attempts, and progress stay on `require_course_owner` (public visitors get 403).

### AWS resources (SAM stack)

| Service | Role |
|---------|------|
| **Cognito User Pool** | Authentication for the SPA |
| **AWS Amplify Hosting** | Frontend hosting and CI/CD deployment from GitHub |
| **API Gateway** | REST API (`/prod` stage), default Cognito authorizer |
| **Lambda** | 19 functions: HTTP handlers, `process_document` (S3), `generate_questions` API + worker (async, no retries) |
| **DynamoDB** | `users`, `courses`, `documents`, `question_sets`, `questions`, `attempts`, `attempt_answers`, `user_progress` |
| **S3** | Raw uploads (`limdocs-raw-uploads-{account}`) and processed text (`limdocs-processed-outputs-{account}`) |
| **Textract** | OCR for uploaded PDFs/images |
| **OpenAI** | Sub-topic extraction after OCR; quiz question generation (structured JSON schema + topic allowlist) |

`process_document` and the quiz worker use **reserved concurrency of 2** (Learner Lab cost guardrail).

### Document processing lifecycle

`documents.processing_status` is document ingestion/processing health only. Quiz generation is a separate state machine on the question-set generation record.

| Status | Meaning |
|--------|---------|
| `UPLOADED` | Metadata recorded; file in S3 |
| `PROCESSING` | Textract job in progress (idempotent claim from `UPLOADED`) |
| `READY` | Text extracted, bilingual `topics` available, eligible for quiz generation |
| `FAILED` / `ERROR` | The document itself could not be processed (unsupported type, S3/Textract failure, unusable content). `failure_reason` when set. |

A quiz-generation request uses its own `generation_id`: `GENERATING` → `READY`, or `GENERATING` → `FAILED` (`failure_code`). That status never writes back to the source documents. A failed generation can be retried immediately on the same `READY` documents. Legacy rows that still store `GENERATING`, or `FAILED` with a quiz `failure_code`, are reported as `READY` on read when processed text exists.

The course page polls materials while any document is not in a terminal processing state (`READY`, `FAILED`, `ERROR`). Quiz completion is determined from the current generation identity, not from document badges.

### Quiz generation modes

| `generation_mode` | When |
|-------------------|------|
| `NORMAL` | Default practice set from selected documents |
| `WEAKNESS_FOCUSED` | `focus_weak_topics: true` and progress data exist—worker prioritizes up to five weakest canonical topics |

Question sets also store `quiz_language`, `requested_question_count`, and (when applicable) `focused_topics` metadata.

### Deletion semantics

Deletes follow **S3 first, then DynamoDB** for documents. Deleting an attempt removes answer rows, subtracts stored `matrix_deltas` from `user_progress` (with rebuild fallback), then deletes the attempt. Deleting a course cascades through documents (raw + processed buckets), question sets, questions, attempts, attempt answers, and the owner's `user_progress` row before removing the course.

---

## Repository layout

```
Limdocs/
├── frontend/              # React + Vite SPA (pages, services, components, i18n)
├── backend/
│   ├── template.yaml      # SAM / CloudFormation
│   ├── src/               # Lambda handlers + shared modules (course_access, openai_helpers, topic_scoring, progress_matrix)
│   └── tests/             # Python unit tests (topic scoring, progress matrix, quiz generation)
├── docs/
│   ├── design.md          # Product & architecture design (text; may be gitignored locally)
│   ├── lambda-functions-flow.md
│   ├── course-documents-plan.md
│   └── progress.log.md
└── package.json           # npm workspaces (runs frontend scripts from root)
```

---

## Local development

### Prerequisites

- Node.js 18+
- Python 3.9+ (matches Lambda runtime in `backend/template.yaml`)
- [AWS CLI](https://aws.amazon.com/cli/) configured
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)

### Frontend

From the repo root:

```bash
npm install
npm run dev
```

Or from `frontend/`:

```bash
cd frontend
npm install
npm run dev
```

Copy `frontend/.env.example` to `frontend/.env.local` and set values from SAM stack outputs after deploy:

```bash
VITE_COGNITO_USER_POOL_ID=<UserPoolId>
VITE_COGNITO_USER_POOL_CLIENT_ID=<UserPoolClientId>
VITE_API_URL=https://<api-id>.execute-api.<region>.amazonaws.com/prod
```

The app also accepts `VITE_API_BASE_URL` as an alias for `VITE_API_URL`. It uses the Cognito **ID token** as `Authorization: Bearer` for API Gateway.

**Routes:** `/login`, `/register`, `/home`, `/course/:courseId` (optional `?tab=` for inner tabs)

### Backend (SAM)

```bash
cd backend
sam build            # Windows: sam build --use-container
sam validate
sam deploy --guided
```

During deploy you will be prompted for **`OpenAIApiKey`** (`NoEcho` parameter). Quiz generation and topic extraction depend on it.

`sam build` must bundle Python dependencies from `backend/src/requirements.txt` (including `openai`). Do not deploy only `.py` sources without building, or workers will fail with `ModuleNotFoundError: no module named 'openai'`.

The stack is configured for **AWS Learner Lab** style accounts (`LabRole` IAM role). Adjust `Role` in `template.yaml` for other environments.

**Key outputs:** `ApiUrl`, `UserPoolClientId`, bucket names, table names (see `Outputs` in `template.yaml`).

**Backend unit tests** (from repo root, with deps installed for `backend/src`):

```bash
cd backend
python -m pytest tests/
```

### Main API surface

All routes require Cognito auth unless noted. Course-scoped reads use owner or public-read access; mutations, attempts, and progress stay owner-only.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/users` | Sync user profile after signup |
| `POST` | `/courses` | Create course (`course_name`, `description`, `is_public`) |
| `GET` | `/courses/public` | List all public courses (sanitized metadata; `is_owner` derived from token `sub`; `owner_username` when present). Reserved literal segment under `/courses/`. |
| `GET` | `/courses/{courseId}` | Course metadata for CoursePage (`course_id`, `course_name`, `visibility`, `is_owner`). Owner or logged-in visitor of a PUBLIC course; 403 for non-owner PRIVATE. |
| `PATCH` | `/courses/{courseId}/visibility` | Owner-only: set `visibility` to `PUBLIC` or `PRIVATE`. Updates the `visibility_courses_index` GSI used by Explore. |
| `GET` | `/users/{userId}/courses` | List caller's courses (`userId` must match token `sub`) |
| `DELETE` | `/courses/{courseId}` | Delete course and all related data |
| `POST` | `/courses/{courseId}/upload-url` | Pre-signed upload + document row (`file_name`, `file_type`, `file_size_bytes`) |
| `GET` | `/courses/{courseId}/materials` | List course documents (owners: full items including `topics`; public visitors: `document_id` + `original_file_name` only) |
| `DELETE` | `/courses/{courseId}/documents/{documentId}` | Delete document |
| `POST` | `/courses/{courseId}/generate-quiz` | Start async question generation (`202`). Body: `document_ids` (required), optional `requested_question_count` (5/10/15/20), `quiz_language` (`he`/`en`, both required if either sent), `focus_weak_topics` (boolean) |
| `GET` | `/courses/{courseId}/question-sets` | List question sets (owner or public visitor) |
| `GET` | `/courses/{courseId}/question-sets/{setId}` | Question set detail + questions (owner or public visitor; does not create an attempt) |
| `DELETE` | `/courses/{courseId}/question-sets/{setId}` | Delete question set |
| `POST` | `/courses/{courseId}/question-sets/{setId}/attempts` | Submit quiz attempt (`answers`, `time_spent_seconds`) |
| `GET` | `/courses/{courseId}/attempts` | List attempts |
| `GET` | `/courses/{courseId}/attempts/{attemptId}/answers` | Attempt review payload |
| `DELETE` | `/courses/{courseId}/attempts/{attemptId}` | Delete attempt and adjust progress |
| `GET` | `/courses/{courseId}/progress` | `{ course_id, matrix, topics }` — `topics` includes weighted scores and status bands |

S3 uploads trigger `process_document` automatically (no HTTP route). The quiz **worker** is invoked asynchronously by the generate-quiz API handler (not exposed on API Gateway).

---

## Technology stack

| Layer | Choices |
|-------|---------|
| **UI** | React 19, Vite 8, CSS (logical properties for RTL/LTR), shared design tokens in `frontend/src/index.css` |
| **Client auth** | `aws-amplify` v6 |
| **Frontend hosting** | AWS Amplify Hosting (GitHub-connected build/deploy) |
| **HTTP** | `axios` service modules under `frontend/src/services/` |
| **IaC** | AWS SAM (`backend/template.yaml`) |
| **Compute** | AWS Lambda (Python 3.9) |
| **Auth** | Amazon Cognito User Pools |
| **API** | Amazon API Gateway (REST) |
| **Storage** | DynamoDB, S3 |
| **OCR** | Amazon Textract (async document text detection) |
| **LLM** | OpenAI API via official Python SDK (`gpt-4.1-mini`) |

---

## Documentation

- [Design document](docs/design.md) — product goals, AWS module mapping, data model notes
- [Lambda functions flow](docs/lambda-functions-flow.md) — trigger map and Mermaid diagram
- [Course documents plan](docs/course-documents-plan.md) — materials UI and API notes
- [Progress log](docs/progress.log.md) — chronological engineering milestones

---

## Authors

- Yarden Vaknin
- Nadav Masliah

*Cloud Computing Workshop with AWS — The Academic College of Tel Aviv-Yafo.*
