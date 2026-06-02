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
  const [password, setPassword] = useState('');
  const [loginError, setLoginError] = useState('');
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

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoginError('');

    try {
      const formData = new URLSearchParams();
      formData.append('username', username);
      formData.append('password', password);

      const response = await fetch(`${AUTH_URL}/api/auth/login`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: formData,
        credentials: 'include',
      });

      if (response.ok) {
        setIsAuthenticated(true);
        setPassword('');
        // After successful login, check session to get username
        const sessionResp = await fetch(`${AUTH_URL}/api/auth/session`, {
          method: 'GET',
          credentials: 'include',
        });
        if (sessionResp.ok) {
          const sessionData = await sessionResp.json();
          setUsername(sessionData.username || '');
        }
        fetchReports();
      } else {
        const data = await response.json();
        setLoginError(data.error || 'Login failed');
      }
    } catch {
      setLoginError('Failed to connect to authentication server');
    }
  };

  const handleLogout = async () => {
    try {
      await fetch(`${AUTH_URL}/api/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      });
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
          <h2 className="text-xl mb-4 text-center text-gray-600">Sign In</h2>
          <form onSubmit={handleLogin}>
            <div className="mb-4">
              <label className="block text-gray-700 text-sm font-bold mb-2" htmlFor="username">
                Username
              </label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-blue-500"
                required
              />
            </div>
            <div className="mb-6">
              <label className="block text-gray-700 text-sm font-bold mb-2" htmlFor="password">
                Password
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded focus:outline-none focus:border-blue-500"
                required
              />
            </div>
            {loginError && (
              <div className="mb-4 text-red-500 text-sm text-center">{loginError}</div>
            )}
            <button
              type="submit"
              className="w-full bg-blue-500 text-white py-2 px-4 rounded hover:bg-blue-600 focus:outline-none"
            >
              Sign In
            </button>
          </form>
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