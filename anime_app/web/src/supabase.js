import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL || 'https://ammwkqeqjvukfuolnkuz.supabase.co';
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY || 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFtbXdrcWVxanZ1a2Z1b2xua3V6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTM4NzEzMDYsImV4cCI6MjEwOTQ0NzMwNn0.qxBcvQkUO-0h15dEho_GFk3WEV6X7xBeKY3DbzFJKRQ';

export const supabase = (supabaseUrl && supabaseAnonKey)
  ? createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  })
  : null;

