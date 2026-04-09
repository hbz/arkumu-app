"""
Tests for upload_utils - utility functions for S3 operations.
"""
import pytest
import boto3
from moto import mock_aws
import os
from unittest.mock import patch

from arkumu.storage.services.upload import upload_utils

# Import test constants
from .test_constants import (
    TEST_BUCKET_NAME, 
    TEST_INGEST_BUCKET, 
    TEST_PRODUCTION_BUCKET,
)


@pytest.fixture
def mock_s3():
    """Set up mock AWS S3 environment"""
    with mock_aws():
        # Create S3 client with mock credentials
        s3 = boto3.client(
            's3',
            aws_access_key_id='testing',
            aws_secret_access_key='testing',
            region_name='us-east-1'
        )
        
        # Create test buckets
        s3.create_bucket(Bucket=TEST_BUCKET_NAME)
        s3.create_bucket(Bucket=TEST_INGEST_BUCKET)
        s3.create_bucket(Bucket=TEST_PRODUCTION_BUCKET)
        
        yield s3


@pytest.fixture
def aws_credentials():
    """Mock AWS credentials for testing"""
    os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
    os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
    os.environ['AWS_SECURITY_TOKEN'] = 'testing'
    os.environ['AWS_SESSION_TOKEN'] = 'testing'
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'


def test_generate_file_key_basic():
    """Test basic file key generation"""
    result = upload_utils.generate_file_key("test.txt")
    assert result == "test.txt"


def test_generate_file_key_with_spaces():
    """Test file key generation with spaces in filename"""
    result = upload_utils.generate_file_key("my test file.txt")
    assert result == "my_test_file.txt"
    assert " " not in result


def test_generate_file_key_with_prefix():
    """Test file key generation with path prefix"""
    result = upload_utils.generate_file_key("test.txt", "uploads/documents")
    assert result == "uploads/documents/test.txt"


def test_generate_file_key_with_prefix_trailing_slash():
    """Test file key generation with prefix that has trailing slash"""
    result = upload_utils.generate_file_key("test.txt", "uploads/documents/")
    assert result == "uploads/documents/test.txt"


def test_generate_file_key_with_prefix_leading_slash():
    """Test file key generation with prefix that has leading slash"""
    result = upload_utils.generate_file_key("test.txt", "/uploads/documents")
    assert result == "uploads/documents/test.txt"


def test_generate_file_key_empty_prefix():
    """Test file key generation with empty prefix"""
    result = upload_utils.generate_file_key("test.txt", "")
    assert result == "test.txt"
    
    result = upload_utils.generate_file_key("test.txt", "   ")
    assert result == "test.txt"


def test_validate_s3_key_valid():
    """Test S3 key validation with valid key"""
    result = upload_utils.validate_s3_key("valid/path/file.txt")
    assert result['valid'] is True
    assert len(result['errors']) == 0


def test_validate_s3_key_too_long():
    """Test S3 key validation with key that's too long"""
    long_key = "a" * 1025  # Longer than 1024 chars
    result = upload_utils.validate_s3_key(long_key)
    assert result['valid'] is False
    assert any("exceeds maximum length" in error for error in result['errors'])


def test_validate_s3_key_invalid_characters():
    """Test S3 key validation with invalid characters"""
    invalid_keys = [
        "path\\with\\backslashes",
        "path{with}braces",
        "path^with^caret",
        "path%with%percent",
        'path"with"quotes'
    ]
    
    for key in invalid_keys:
        result = upload_utils.validate_s3_key(key)
        assert result['valid'] is False
        assert any("invalid character" in error for error in result['errors'])


def test_validate_s3_key_warnings():
    """Test S3 key validation warnings"""
    # Leading slash
    result = upload_utils.validate_s3_key("/leading/slash")
    assert result['valid'] is True
    assert any("starts with '/'" in warning for warning in result['warnings'])
    
    # Double slashes
    result = upload_utils.validate_s3_key("double//slash")
    assert result['valid'] is True
    assert any("double slashes" in warning for warning in result['warnings'])
    
    # Spaces
    result = upload_utils.validate_s3_key("path with spaces")
    assert result['valid'] is True
    assert any("contains spaces" in warning for warning in result['warnings'])


def test_normalize_s3_key():
    """Test S3 key normalization"""
    # Remove leading slash
    assert upload_utils.normalize_s3_key("/path/file.txt") == "path/file.txt"
    
    # Replace spaces with underscores
    assert upload_utils.normalize_s3_key("my file.txt") == "my_file.txt"
    
    # Fix double slashes
    assert upload_utils.normalize_s3_key("path//with//doubles") == "path/with/doubles"
    
    # Remove trailing slash
    assert upload_utils.normalize_s3_key("path/to/folder/") == "path/to/folder"
    
    # All together
    normalized = upload_utils.normalize_s3_key("/path//with spaces/file.txt/")
    assert normalized == "path/with_spaces/file.txt"


def test_build_s3_url_default():
    """Test building S3 URL with default AWS endpoint"""
    url = upload_utils.build_s3_url("my-bucket", "path/file.txt")
    expected = "https://my-bucket.s3.amazonaws.com/path/file.txt"
    assert url == expected


def test_build_s3_url_custom_endpoint():
    """Test building S3 URL with custom endpoint (MinIO)"""
    url = upload_utils.build_s3_url(
        "my-bucket", 
        "path/file.txt", 
        "http://localhost:9020"
    )
    expected = "http://localhost:9020/my-bucket/path/file.txt"
    assert url == expected


def test_build_s3_url_custom_endpoint_trailing_slash():
    """Test building S3 URL with custom endpoint that has trailing slash"""
    url = upload_utils.build_s3_url(
        "my-bucket", 
        "path/file.txt", 
        "http://localhost:9020/"
    )
    expected = "http://localhost:9020/my-bucket/path/file.txt"
    assert url == expected


def test_calculate_multipart_info_small_file():
    """Test multipart calculation for small file"""
    file_size = 50 * 1024 * 1024  # 50MB
    result = upload_utils.calculate_multipart_info(file_size)
    
    assert result['should_use_multipart'] is False
    assert result['total_size'] == file_size
    assert result['part_count'] >= 1
    assert result['chunk_size'] >= 5 * 1024 * 1024  # At least 5MB


def test_calculate_multipart_info_large_file():
    """Test multipart calculation for large file"""
    file_size = 500 * 1024 * 1024  # 500MB
    result = upload_utils.calculate_multipart_info(file_size)
    
    assert result['should_use_multipart'] is True
    assert result['total_size'] == file_size
    assert result['part_count'] <= 10000  # S3 limit
    assert result['chunk_size'] >= 5 * 1024 * 1024  # At least 5MB


def test_calculate_multipart_info_huge_file():
    """Test multipart calculation for huge file that needs chunk size adjustment"""
    file_size = 100 * 1024 * 1024 * 1024  # 100GB
    result = upload_utils.calculate_multipart_info(file_size)
    
    assert result['should_use_multipart'] is True
    assert result['part_count'] <= 10000
    assert result['chunk_size'] >= 5 * 1024 * 1024


def test_calculate_multipart_info_custom_chunk_size():
    """Test multipart calculation with custom chunk size"""
    file_size = 200 * 1024 * 1024  # 200MB
    chunk_size = 20 * 1024 * 1024  # 20MB
    result = upload_utils.calculate_multipart_info(file_size, chunk_size)
    
    assert result['chunk_size'] == chunk_size
    assert result['part_count'] == 10  # 200MB / 20MB


def test_calculate_multipart_info_small_chunk_adjustment():
    """Test that small chunk sizes get adjusted to minimum"""
    file_size = 100 * 1024 * 1024  # 100MB
    small_chunk = 1024 * 1024  # 1MB (below minimum)
    result = upload_utils.calculate_multipart_info(file_size, small_chunk)
    
    assert result['chunk_size'] == 5 * 1024 * 1024  # Should be adjusted to 5MB minimum


def test_format_upload_progress():
    """Test upload progress formatting"""
    import time
    start_time = time.time()
    current_time = start_time + 10  # 10 seconds later
    
    result = upload_utils.format_upload_progress(
        uploaded_bytes=1024 * 1024,  # 1MB
        total_bytes=10 * 1024 * 1024,  # 10MB
        start_time=start_time,
        current_time=current_time
    )
    
    assert result['progress_percent'] == 10.0
    assert result['uploaded_bytes'] == 1024 * 1024
    assert result['total_bytes'] == 10 * 1024 * 1024
    assert 'uploaded_formatted' in result
    assert 'total_formatted' in result
    assert 'speed_bps' in result
    assert 'speed_formatted' in result
    assert result['duration'] == 10.0
    assert 'eta_seconds' in result


def test_format_upload_progress_complete():
    """Test upload progress formatting when complete"""
    import time
    start_time = time.time()
    current_time = start_time + 5
    
    result = upload_utils.format_upload_progress(
        uploaded_bytes=5 * 1024 * 1024,  # 5MB
        total_bytes=5 * 1024 * 1024,     # 5MB (complete)
        start_time=start_time,
        current_time=current_time
    )
    
    assert result['progress_percent'] == 100.0
    assert result['eta_seconds'] == 0


@pytest.mark.django_db
def test_safe_head_object_success(mock_s3, aws_credentials):
    """Test safe_head_object with successful response"""
    env_vars = {
        'S3_INGEST_BUCKET': TEST_INGEST_BUCKET,
        'S3_PRODUCTION_BUCKET': TEST_PRODUCTION_BUCKET,
        'AWS_ACCESS_KEY_ID': 'testing',
        'AWS_SECRET_ACCESS_KEY': 'testing',
        'AWS_DEFAULT_REGION': 'us-east-1'
    }
    if 'AWS_S3_ENDPOINT_URL' in os.environ:
        env_vars['AWS_S3_ENDPOINT_URL'] = ''
    
    with patch.dict(os.environ, env_vars):
        # First upload a file
        mock_s3.put_object(
            Bucket=TEST_INGEST_BUCKET,
            Key='test-head/file.txt',
            Body=b'test content'
        )
        
        # Mock the BaseStorageService to return our mocked S3 client
        with patch('arkumu.storage.services.upload.upload_utils.BaseStorageService') as mock_base_service:
            mock_base_service.return_value.s3_client = mock_s3
            
            # Test safe_head_object
            result = upload_utils.safe_head_object(TEST_INGEST_BUCKET, 'test-head/file.txt')
            
            assert result is not None
            assert 'ContentLength' in result
            assert result['ContentLength'] == len(b'test content')


@pytest.mark.django_db 
def test_safe_head_object_not_found(mock_s3, aws_credentials):
    """Test safe_head_object with non-existent file"""
    env_vars = {
        'S3_INGEST_BUCKET': TEST_INGEST_BUCKET,
        'S3_PRODUCTION_BUCKET': TEST_PRODUCTION_BUCKET,
        'AWS_ACCESS_KEY_ID': 'testing',
        'AWS_SECRET_ACCESS_KEY': 'testing',
        'AWS_DEFAULT_REGION': 'us-east-1'
    }
    if 'AWS_S3_ENDPOINT_URL' in os.environ:
        env_vars['AWS_S3_ENDPOINT_URL'] = ''
    
    with patch.dict(os.environ, env_vars):
        result = upload_utils.safe_head_object(TEST_INGEST_BUCKET, 'nonexistent/file.txt')
        assert result is None


@pytest.mark.django_db
def test_get_file_info_success(mock_s3, aws_credentials):
    """Test get_file_info with existing file"""
    env_vars = {
        'S3_INGEST_BUCKET': TEST_INGEST_BUCKET,
        'S3_PRODUCTION_BUCKET': TEST_PRODUCTION_BUCKET,
        'AWS_ACCESS_KEY_ID': 'testing',
        'AWS_SECRET_ACCESS_KEY': 'testing',
        'AWS_DEFAULT_REGION': 'us-east-1'
    }
    if 'AWS_S3_ENDPOINT_URL' in os.environ:
        env_vars['AWS_S3_ENDPOINT_URL'] = ''
    
    with patch.dict(os.environ, env_vars):
        # Upload a test file
        test_content = b'test content for file info'
        mock_s3.put_object(
            Bucket=TEST_INGEST_BUCKET,
            Key='info-test/file.txt',
            Body=test_content,
            ContentType='text/plain'
        )
        
        # Mock the BaseStorageService to return our mocked S3 client
        with patch('arkumu.storage.services.upload.upload_utils.BaseStorageService') as mock_base_service:
            mock_base_service.return_value.s3_client = mock_s3
            mock_base_service.return_value.ingest_bucket = TEST_INGEST_BUCKET
            
            result = upload_utils.get_file_info('info-test/file.txt', TEST_INGEST_BUCKET)
            
            assert result['success'] is True
            assert result['s3_key'] == 'info-test/file.txt'
            assert result['file_name'] == 'file.txt'
            assert result['bucket'] == TEST_INGEST_BUCKET
            assert result['file_size'] == len(test_content)
            assert 'content_type' in result
            assert 'last_modified' in result


@pytest.mark.django_db
def test_get_file_info_not_found(mock_s3, aws_credentials):
    """Test get_file_info with non-existent file"""
    env_vars = {
        'S3_INGEST_BUCKET': TEST_INGEST_BUCKET,
        'S3_PRODUCTION_BUCKET': TEST_PRODUCTION_BUCKET,
        'AWS_ACCESS_KEY_ID': 'testing',
        'AWS_SECRET_ACCESS_KEY': 'testing',
        'AWS_DEFAULT_REGION': 'us-east-1'
    }
    if 'AWS_S3_ENDPOINT_URL' in os.environ:
        env_vars['AWS_S3_ENDPOINT_URL'] = ''
    
    with patch.dict(os.environ, env_vars):
        # Mock the BaseStorageService to return our mocked S3 client
        with patch('arkumu.storage.services.upload.upload_utils.BaseStorageService') as mock_base_service:
            mock_base_service.return_value.s3_client = mock_s3
            mock_base_service.return_value.ingest_bucket = TEST_INGEST_BUCKET
            
            result = upload_utils.get_file_info('nonexistent/file.txt', TEST_INGEST_BUCKET)
            
            assert result['success'] is False
            assert result['error'] == 'File not found'
            assert result['s3_key'] == 'nonexistent/file.txt'