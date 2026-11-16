name: Bug Report
description: Create a report to help us improve OmniSync-Downloader
labels: ["bug"]
body:
  - type: textarea
    attributes:
      label: Description
      description: A clear and concise description of the bug.
    validations:
      required: true
  - type: textarea
    attributes:
      label: Steps to Reproduce
      description: Steps to reproduce the behavior.
    validations:
      required: true
  - type: input
    attributes:
      label: OS / Environment
      placeholder: Windows 11 / Python 3.12
