---
title: Privacy policy - paperwala-pipeline
---

# Privacy policy

*Last updated: 28 October 2026*

paperwala-pipeline is an internal tool used by Paperwala's maintainer to clean
exam-paper PDFs. It is not offered to the public, and only the maintainer's own
Google account signs in to it.

## What Google data it accesses

When the maintainer signs in, the tool requests two Google Drive permissions:

- **See and download your Google Drive files** (`drive.readonly`): used only to
  download exam-paper PDFs that Paperwala already links to.
- **See, edit, create and delete only the specific Google Drive files you use with
  this app** (`drive.file`): used only to upload the cleaned exam papers and the
  folders that hold them. The tool cannot change or delete any other file.

## How the data is used

- Exam papers are downloaded, cleaned and uploaded back to the maintainer's Drive.
  Nothing else in the Drive is read, changed or deleted.
- No personal information is collected. The tool keeps only each paper's file id,
  processing status and file checksums.
- Google user data is not sold, shared with third parties, used for advertising,
  or used to train AI models.
- Text read from exam papers may be sent to an AI service (Google Gemini or Groq)
  to identify the paper's subject, code and exam date. That text comes from
  public exam papers and contains no personal data.

The tool's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Storage and security

The sign-in token is stored as an encrypted secret in the project's GitHub
repository and on the maintainer's own computer. It is never published.

## Revoking access

Access can be revoked at any time at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Contact

Questions about this policy: open an issue at
[github.com/krishnakantnagina/paperwala-pipeline/issues](https://github.com/krishnakantnagina/paperwala-pipeline/issues).
