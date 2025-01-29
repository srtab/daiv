import warnings

# Add warning filter to suppress the specific warning
warnings.filterwarnings("ignore", message='directory "/run/secrets" does not exist')
