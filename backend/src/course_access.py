"""Course ownership gate for Cognito-authenticated API Lambdas (same src/ package)."""


def require_course_owner(courses_table, course_id, user_sub):
    """
    Returns None if user_sub owns the course.
    Otherwise returns (http_status, body_dict) for 404 missing course or 403 mismatch.
    """
    result = courses_table.get_item(Key={"course_id": course_id})
    item = result.get("Item")
    if not item:
        return (404, {"message": "Course not found"})
    if item.get("owner_id") != user_sub:
        return (403, {"message": "Forbidden"})
    return None
