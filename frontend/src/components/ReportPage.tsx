import React, { useState, useEffect, useCallback } from 'react';

interface Report {
  id: number;
  title: string;
  content: string;
  created_at: string;
}

const API_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';
const AUTH_URL = process.env.REACT_APP_AUTH_URL || 'http://localhost:8081';

function ReportPage() {
  const [reports, setReports] = useState<Report[]>([]);
  const [error, setError] = useState<string>('');
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [username, setUsername] = useState('');
  const [loading, setLoading] = useState(true);

  const checkAuth = useCallback(async () => {
    try {
      const response = await fetch(`${AUTH_URL}/api/auth/session`, {
        method: 'GET',
        credentials: 'include',
      });
      if (response.ok) {
        const data = await response.json();
        setIsAuthenticated(true);
        setUsername(data.username || '');
        return true;
      } else {
        setIsAuthenticated(false);
        setUsername('');
        return false;
      }
    } catch {
      setIsAuthenticated(false);
      setUsername('');
      return false;
    }
  }, []);

  useEffect(() => {
    checkAuth().finally(() => setLoading(false));
  }, [checkAuth]);

  const fetchReports = useCallback(async () => {
    try {
      const response = await fetch(`${API_URL}/reports/`, {
        method: 'GET',
        credentials: 'include',
      });
      if (response.ok) {
        const data = await response.json();
        setReports(data);
        setError('');
      } else if (response.status === 401) {
        setIsAuthenticated(false);
        setError('Session expired. Please log in again.');
      } else {
        const errData = await response.json();
        setError(errData.detail || 'Failed to fetch reports');
      }
    } catch (err) {
      setError('Failed to connect to server');
    }
  }, []);

  const handleLogin = () => {
    // Redirect browser directly to auth endpoint which will redirect to Keycloak
    window.location.href = `${AUTH_URL}/api/auth/login`;
  };

  const handleLogout = async () => {
    try {
      const response = await fetch(`${AUTH_URL}/api/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      });
      const data = await response.json();
      // If the backend returned a Keycloak logout URL, redirect the browser
      // to end the Keycloak SSO session. Otherwise just reset local state.
      if (data.logout_url) {
        window.location.href = data.logout_url;
        return;
      }
    } catch {
      // ignore
    }
    setIsAuthenticated(false);
    setUsername('');
    setReports([]);
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-gray-500">Loading...</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="bg-white p-8 rounded-lg shadow-md w-96">
          <h1 className="text-2xl font-bold mb-6 text-center">BionicPRO</h1>
          <h2 className="text-xl mb-4 text-center text-gray-600">Authentication Required</h2>
          <p className="mb-6 text-gray-600 text-center">
            You will be redirected to Keycloak for authentication with OTP.
          </p>
          {error && (
            <div className="mb-4 text-red-500 text-sm text-center">{error}</div>
          )}
          <button
            onClick={handleLogin}
            className="w-full bg-blue-500 text-white py-2 px-4 rounded hover:bg-blue-600 focus:outline-none"
          >
            Sign In with Keycloak
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold">BionicPRO Reports</h1>
        <div className="flex items-center gap-4">
          <span className="text-gray-600">Welcome, {username}</span>
          <button
            onClick={handleLogout}
            className="bg-red-500 text-white py-2 px-4 rounded hover:bg-red-600"
          >
            Logout
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-4">
          {error}
        </div>
      )}

      <button
        onClick={fetchReports}
        className="bg-green-500 text-white py-2 px-4 rounded hover:bg-green-600 mb-6"
      >
        Fetch Reports
      </button>

      {reports.length === 0 && !error ? (
        <p className="text-gray-500 text-center mt-8">
          Click "Fetch Reports" to load reports
        </p>
      ) : (
        <div className="grid gap-6">
          {reports.map((report) => (
            <div key={report.id} className="bg-white p-6 rounded-lg shadow">
              <h2 className="text-xl font-semibold mb-2">{report.title}</h2>
              <p className="text-gray-700 mb-2">{report.content}</p>
              <p className="text-sm text-gray-500">
                Created: {new Date(report.created_at).toLocaleDateString()}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default ReportPage;