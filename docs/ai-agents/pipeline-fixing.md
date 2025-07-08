# 🔧 Pipeline Fixing

The Pipeline Fixer is an AI agent that troubleshoots failed pipelines and tries to fix them automatically.

## Job Exclusions

The Pipeline Fixer Agent can be configured to exclude specific CI/CD jobs from automatic fixing. This provides granular control over which pipeline components remain under manual control.

### Common Use Cases

**Security and Compliance:**
- Security scanning jobs that require manual review
- Compliance validation steps that need human oversight
- Vulnerability assessment jobs with regulatory requirements

**Critical Operations:**
- Production deployment jobs requiring manual approval
- Database migration scripts needing careful review
- Infrastructure provisioning jobs with high impact

**Custom Configurations:**
- Jobs with specialized environmental requirements
- Legacy systems with complex manual setup procedures
- Jobs requiring specific credentials or manual intervention

### Configuration

To exclude jobs from automatic fixing, configure the `pipeline.excluded_job_patterns` option in your `.daiv.yml` file. See [Repository Configurations](../getting-started/repository-configurations.md#configure-pipeline-behavior) for detailed setup instructions.

### Benefits

- **Enhanced Security:** Maintain manual control over sensitive operations
- **Compliance:** Meet regulatory requirements for human oversight
- **Risk Management:** Prevent automated changes to critical systems
- **Flexibility:** Customize automation level per pipeline component

